"""Refresh unchanged committed tester work after an accepted source correction."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import audit, checkpoints, db, gates, host_completion, repo, workspace_registry
from .integrator import ensure_integration_branch
from .locking import exclusive


def _git(work, *args):
    return subprocess.run(["git", *args], cwd=work, capture_output=True, check=True, timeout=120).stdout


def refresh(conn, project, task_id, source_task_id):
    return _refresh(conn, project, task_id, source_task_id, source_mode=False)


def refresh_source(conn, project, task_id, source_task_id):
    """Prepare accepted dependencies, keeping the source draft held for its fix."""
    return _refresh(conn, project, task_id, source_task_id, source_mode=True)


def _refresh(conn, project, task_id, source_task_id, *, source_mode):
    """Host-only: archive the old head, carry unchanged tests, run the declared gate.

    Approval is still independent and exact-commit. Conflicts preserve the backup
    and abort the rebase; no source builder or paid tester performs this operation.
    """
    label = "source" if source_mode else "test"
    with exclusive(project.root, f"refresh-tests-{task_id}"):
        task, source = db.get_task(conn, task_id), db.get_task(conn, source_task_id)
        if not task or task["status"] != "BLOCKED":
            raise ValueError("Refresh requires a held task")
        if source_mode:
            if task["kind"] in ("TEST_ONLY", "RESEARCH", "REVIEW", "OPERATOR"):
                raise ValueError("Source refresh requires a held source builder")
        elif task["kind"] != "TEST_ONLY":
            raise ValueError("Refresh requires a held TEST_ONLY task")
        host_completion.authority(conn, project, task)
        from .manager_state import pending
        if pending(conn, task["job_id"]):
            raise PermissionError("Manager recovery audit must precede test refresh")
        if not source or source["status"] != "DONE" or source.get("job_id") != task.get("job_id"):
            raise ValueError("Source correction must be integrated in the same job")
        work = Path(task["worktree"]).resolve(strict=True)
        old = repo.head_commit(work)
        if not repo.is_clean(work) or repo.current_branch(work) != task["branch"]:
            raise ValueError("Preserved test branch must be clean and unchanged")
        if old != task.get("last_commit") or not audit.audit_worktree(conn, project, work, task_id, record=False).clean:
            raise PermissionError("Preserved exact commit/scope must be inspected first")
        base = ensure_integration_branch(project)
        base = repo.head_commit(project.root) if base == "HEAD" else _git(work, "rev-parse", base).decode().strip()
        if not source.get("last_commit") or not repo.is_ancestor(work, source["last_commit"], base):
            raise ValueError("Integration base lacks the accepted source correction")
        paths = repo.diff_files(work, task["base_sha"])
        if not paths:
            raise ValueError("No committed tests to carry")
        blobs = {path: _git(work, "show", old + ":" + path) for path in paths}
        backup = f"archive/agentkit/{label}-{task_id}-{old[:12]}"
        if repo.branch_exists(work, backup):
            if _git(work, "rev-parse", backup).decode().strip() != old:
                raise ValueError("Backup branch differs from preserved test commit")
        else:
            _git(work, "branch", backup, old)
        db.log_event(conn, task_id, f"{label}_refresh_prepared", cause="accepted source correction",
                     detail={"old_head": old, "old_base": task["base_sha"], "new_base": base,
                             "backup": backup, "source_task": source_task_id})
        try:
            _git(work, "rebase", "--onto", base, task["base_sha"])
        except subprocess.CalledProcessError:
            _git(work, "rebase", "--abort")
            raise ValueError("Test refresh conflicted; old branch and archive retained") from None
        head = repo.head_commit(work)
        if any(_git(work, "show", head + ":" + path) != blob for path, blob in blobs.items()):
            raise PermissionError("Test contents changed during refresh; archive retained for inspection")
        db.update_task(conn, task_id, base_sha=base, last_commit=head)
        refreshed = db.get_task(conn, task_id)
        assert refreshed is not None
        workspace_registry.record(project.root, refreshed, phase="preserved", head=head)
        if not audit.audit_worktree(conn, project, work, task_id, record=False).clean:
            raise PermissionError("Refreshed test scope failed; independent repair required")
        # Stay held during host checks; no synthetic live worker or expiring lease.
        result = gates.run_gate(project, task["gate_level"], cwd=work)
        db.record_gate(conn, task_id, task["gate_level"], head, result.passed, result.summary())
        if not source_mode:
            with db.immediate_transaction(conn):
                db.set_status(conn, task_id, "VERIFYING", actor="scheduler", cause="Host carried byte-identical independent tests onto accepted source")
                db.set_status(conn, task_id, "REVIEW" if result.passed else "FAILED", actor="scheduler", cause=result.summary())
        else:
            from .reviews import invalidate
            invalidate(conn, task_id, head, "Source draft rebased; explicit correction and exact review still required")
        checkpoints.write_mechanical(conn, project.root, work, task_id, f"host_{label}_refresh")
        db.log_event(conn, task_id, f"{label}_refresh_completed", detail={"head": head, "backup": backup,
                     "source_task": source_task_id, "unchanged_test_files": paths, "passed": result.passed})
        return {"head": head, "backup": backup, "passed": result.passed, "summary": result.summary()}
