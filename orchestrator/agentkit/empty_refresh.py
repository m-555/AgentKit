"""Host preparation: advance only a stopped checkout with no task work."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import checkpoints, db, host_completion, manager_state, repo, workspace_registry
from .integrator import ensure_integration_branch
from .locking import exclusive
from .worktree_digest import fingerprint


def refresh(conn, project, task_id):
    """Keep branch/path/environment and all old evidence; never rebase worker work.

    A journal permits recovery if the host stops between the Git fast-forward
    and its database checkpoint. Quota recovery proofs are never rewritten.
    This does not requeue, approve or launch the held task.
    """
    if os.environ.get("AGENTKIT_TASK") or os.environ.get("AGENTKIT_PROCESS"):
        raise PermissionError("checkout preparation belongs to the host")
    with exclusive(project.root, "scheduler"), exclusive(project.root, f"refresh-empty-{task_id}"):
        task = db.get_task(conn, task_id)
        preparation_hold = bool(task and task["status"] == "READY"
            and str(task.get("blocker") or "").startswith("Host preparation blocked:"))
        if not task or (task["status"] not in ("BLOCKED", "NEEDS_REPLAN")
                        and not preparation_hold):
            raise ValueError("A held task or stopped READY preparation failure is required")
        host_completion.authority(conn, project, task)
        if manager_state.pending(conn, task.get("job_id")):
            raise PermissionError("Manager recovery audit is required")
        if conn.execute("SELECT 1 FROM recovery_intents i JOIN recovery_sessions s "
                        "ON s.id=i.session_id WHERE s.task_id=? "
                        "AND i.state NOT IN ('RECOVERED','CANCELLED','NEEDS_USER_ACTION')",
                        (task_id,)).fetchone():
            raise PermissionError("Pending recovery proof must be preserved")
        work = Path(task["worktree"]).resolve(strict=True)
        entries = repo.worktree_list(project.root)
        if not any(Path(e.get("worktree", "")).resolve() == work for e in entries):
            raise ValueError("Checkout is not registered in this repository")
        forbidden = {"main", "master", ensure_integration_branch(project), *project.raw.get("protected_branches", [])}
        if task.get("branch") in forbidden:
            raise PermissionError("Protected/integration branches cannot be refreshed as workers")
        old = task.get("base_sha")
        head = repo.head_commit(work)
        if not old or not repo.is_clean(work) or repo.current_branch(work) != task.get("branch"):
            raise ValueError("Checkout must be clean on its recorded branch")
        if task.get("last_commit") not in (None, old):
            raise ValueError("Committed worker work must be preserved")
        records = db.recent_events(conn, task_id=task_id, kind="empty_refresh_prepared", limit=1)
        prepared = records[0]["detail"] if records else {}
        saved = (db.latest_checkpoint(conn, task_id, kind="mechanical") or {}).get("payload") or {}
        recovering = (prepared.get("target") == head and prepared.get("branch") == task["branch"]
                      and (prepared.get("old_base") == old or old == head)
                      and (head != old or (saved and saved.get("head_sha") != head)))
        if head != old and not recovering:
            raise ValueError("Checkout contains worker commits or unexplained drift")
        if not recovering:
            if saved and (saved.get("head_sha") != head or saved.get("dirty_digest") != fingerprint(work)):
                raise PermissionError("Stopped checkout evidence changed")
            branch = ensure_integration_branch(project)
            target = repo.rev_parse(work, branch)
            if target == head:
                return {"changed": False, "head": head}
            if not repo.is_ancestor(work, old, target):
                raise ValueError("Integration no longer descends from the checkout base")
            if preparation_hold:
                db.set_status(conn, task_id, "BLOCKED", actor="human",
                              cause="Host holds pristine preparation failure for refresh")
            db.log_event(conn, task_id, "empty_refresh_prepared", detail={
                "old_base": old, "target": target, "branch": task["branch"], "ai_calls": 0})
            subprocess.run(["git", "merge", "--ff-only", target], cwd=work,
                           capture_output=True, check=True, timeout=120)
            head = repo.head_commit(work)
            if head != target or not repo.is_clean(work):
                raise PermissionError("Host refresh needs inspection before launch")
        db.update_task(conn, task_id, base_sha=head, last_commit=head)
        checkpoints.write_mechanical(conn, project.root, work, task_id, reason="host pristine checkout refresh")
        db.log_event(conn, task_id, "empty_refresh_completed", detail={
            "old_base": old, "head": head, "ai_calls": 0, "recovered_journal": recovering})
        workspace_registry.record(project.root, task, phase="preserved", head=head)
        return {"changed": True, "head": head, "old_base": old}
