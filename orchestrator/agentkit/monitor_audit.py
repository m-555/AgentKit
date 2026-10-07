"""Strict-worker L5 runs in the host monitor, outside virtual sandbox mounts."""
from __future__ import annotations

from . import audit, db, workflow
from .config import load_project


def check(conn, root, process, worktree):
    project = load_project(root)
    if process["purpose"] != "worker" or not workflow.enabled(project):
        return ""
    task = db.get_task(conn, process["task_id"])
    if not task or task["generation"] != process["generation"] or task["status"] != "RUNNING":
        return ""
    try:
        result = audit.audit_worktree(conn, project, worktree, task["id"])
        if result.clean:
            return ""
        reason = "[AgentKit host lease audit] " + result.summary()
    except (OSError, ValueError) as exc:
        reason = "[AgentKit host lease audit unavailable] " + str(exc)
    db.update_task(conn, task["id"], blocker=reason,
                   next_action="Manager must inspect preserved work and the host audit before retry.")
    db.set_status(conn, task["id"], "BLOCKED", cause=reason)
    return reason
