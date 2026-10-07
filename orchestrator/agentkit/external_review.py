"""Exact-commit review by an authenticated external manager session."""
from __future__ import annotations

import os

from . import db, manager, manager_state, reviews
from .mcp_manager import credential


def enabled(project) -> bool:
    return project.raw.get("review_mode") == "external-manager"


def submit(conn, project, task_id: int, head_sha: str, verdict: str, evidence: str) -> None:
    if not enabled(project):
        raise PermissionError("external-manager review is not configured")
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("a supervised worker cannot impersonate the external manager")
    task = db.get_task(conn, task_id)
    if not task or not task.get("job_id"):
        raise ValueError("review requires a job task")
    token = credential(project.root, task["job_id"])
    if not token:
        raise PermissionError("external manager is not attached")
    lease = manager.require_lease(conn, task["job_id"], token)
    from .external_identity import require
    session = require(lease)
    if manager_state.pending(conn, task["job_id"]):
        raise PermissionError("manager recovery audit must be acknowledged before review")
    actor = {"id": -1, "purpose": "external-manager", "session_token": session}
    reviews.ensure_independent(conn, task, actor)
    reviews.approve(conn, project, task_id, head_sha, verdict.upper(),
                    f"external-manager:{lease['holder']}", evidence)
