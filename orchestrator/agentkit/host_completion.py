"""Finish preserved worker files on the host; never impersonate an AI session."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import (
    audit,
    checkpoints,
    db,
    gates,
    manager,
    manager_audit,
    processes,
    repo,
    workspace_registry,
)
from .commit_format import normalize
from .git_staging import stage
from .locking import exclusive
from .mcp_manager import credential
from .worktree_digest import fingerprint


def authority(conn, project, task):
    if os.environ.get("AGENTKIT_TASK") or os.environ.get("AGENTKIT_PROCESS"):
        raise PermissionError("host recovery cannot originate from a worker")
    token = credential(project.root, task["job_id"])
    if not token:
        raise PermissionError("external manager is not attached")
    lease = manager.require_lease(conn, task["job_id"], token)
    from .external_identity import require
    require(lease)
    manager_audit.authority(conn, project.root, task["job_id"], token)
    if any(p["task_id"] == task["id"] for p in processes.owning(conn)):
        raise PermissionError("previous monitor or child may still own this task")
    return lease["holder"]


def validate_preserved(conn, project, task):
    budget_stop = task["status"] == "BLOCKED" and str(task.get("blocker") or "").startswith("[AgentKit execution limit]")
    if (task["status"] not in ("FAILED", "STALE") and not budget_stop) or not task.get("worktree") or not task.get("base_sha"):
        raise ValueError("host completion requires failed preserved worker work")
    work = Path(task["worktree"]).resolve(strict=True)
    saved = (db.latest_checkpoint(conn, task["id"], kind="mechanical") or {}).get("payload") or {}
    if (repo.current_branch(work) != task.get("branch")
            or not repo.is_ancestor(work, task["base_sha"], "HEAD")
            or saved.get("generation") != task["generation"]
            or saved.get("head_sha") != repo.head_commit(work)
            or saved.get("dirty_digest") != fingerprint(work)):
        raise ValueError("preserved worker evidence changed; inspect and checkpoint explicitly")
    result = audit.audit_worktree(conn, project, work, task["id"], record=False)
    if not result.clean:
        raise PermissionError(result.summary())
    if not repo.changed_files(work):
        head = repo.head_commit(work)
        if task.get("last_commit") != head or head == task["base_sha"]:
            raise ValueError("no exact recorded committed candidate to recheck")
    return work


def complete(conn, project, task_id: int, message: str) -> str:
    """Commit exact preserved edits and verify them; independent review follows."""
    if not message.strip() or len(message) > 2000 or "\0" in message:
        raise ValueError("commit message must contain 1-2000 characters")
    with exclusive(project.root, f"commit-task-{task_id}"):
        task = db.get_task(conn, task_id)
        if not task:
            raise ValueError("task not found")
        holder = authority(conn, project, task)
        work = validate_preserved(conn, project, task)
        from .worker_preparation import validate_completed
        validate_completed(project, task, work)
        from .environment_prepare import prepare
        prepared = prepare(project, work, task)
        if not prepared.passed:
            raise ValueError(prepared.summary())
        db.acquire_leases(conn, task_id, task["owned_paths"], generation=task["generation"])
        try:
            db.set_status(conn, task_id, "VERIFYING", actor="human", cause="host finishing preserved edits; no AI retry")
            def guard():
                current = db.get_task(conn, task_id)
                if not current or current["generation"] != task["generation"] or current["status"] != "VERIFYING":
                    raise PermissionError("host recovery lost task authority")
                authority(conn, project, current)
                if repo.current_branch(work) != task["branch"]:
                    raise PermissionError("assigned branch changed")
            changed = repo.changed_files(work)
            normalize(project, work, changed, guard)
            # Project static gates run before committing, as on worker commits.
            before = gates.run_gate(project, "fast", cwd=work)
            if not before.passed:
                raise ValueError(before.summary())
            guard()
            result = audit.audit_worktree(conn, project, work, task_id)
            if not result.clean:
                raise PermissionError(result.summary())
            if repo.changed_files(work):
                stage(work, repo.changed_files(work))
                result = audit.audit_staged(conn, project, work, task_id)
                if not result.clean:
                    raise PermissionError(result.summary())
                guard()
                # Git's existing L6 hook needs the assigned task identity. This is
                # an explicitly labelled host operation, not a worker/model session.
                git_env = {**os.environ, "AGENTKIT_ROOT": str(project.root),
                           "AGENTKIT_TASK": str(task_id), "AGENTKIT_GENERATION": str(task["generation"]),
                           "AGENTKIT_ROLE": "host-completion", "AGENTKIT_WORKTREE": str(work)}
                committed = subprocess.run(["git", "commit", "-m", message], cwd=work, env=git_env,
                                           capture_output=True, text=True, timeout=120)
                if committed.returncode:
                    raise ValueError("Host commit rejected: " + committed.stderr[-2000:])
            head = repo.head_commit(work)
            db.update_task(conn, task_id, last_commit=head)
            from .verification import run
            gate = run(conn, project, work, task["gate_level"])
            passed = gate.passed and repo.is_clean(work) and repo.head_commit(work) == head
            db.record_gate(conn, task_id, task["gate_level"], head, passed, gate.summary())
            if not passed:
                raise ValueError(gate.summary())
            guard()
            db.set_status(conn, task_id, "REVIEW", actor="human", cause="host commit and declared gate passed")
            db.update_task(conn, task_id, blocker=None, next_action="Independent review of host-finished worker commit required")
            workspace_registry.record(project.root, task, phase="committed", head=head)
            checkpoints.write_mechanical(conn, project.root, work, task_id, "host_completion")
            db.log_event(conn, task_id, "host_completion", detail={"head": head, "holder": holder, "ai_calls": 0})
            return head
        except Exception as error:
            current = db.get_task(conn, task_id)
            if current and current["status"] == "VERIFYING":
                db.set_status(conn, task_id, "FAILED", actor="human", cause="host completion stopped: " + str(error)[:1000])
                db.update_task(conn, task_id, blocker=str(error)[:1000])
                checkpoints.write_mechanical(conn, project.root, work, task_id, "host_completion_stopped")
            raise
        finally:
            db.release_leases(conn, task_id, reason="host completion ended")
