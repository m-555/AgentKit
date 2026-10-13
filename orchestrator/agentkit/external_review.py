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


def standin_note(conn, project, job_id: str) -> str:
    """The coordinator prompt line for reviews the absent external manager left waiting (F29)."""
    if not enabled(project):
        return ""
    waiting = [str(t["id"]) for t in db.list_tasks(conn, ("REVIEW",)) if t.get("job_id") == job_id]
    if not waiting:
        return ""
    return (f"\nTasks {', '.join(waiting)} wait in REVIEW for the external manager, who is absent. Stand in: "
            "for each, read brief, task_diff and audit_diff at its exact head, judge it against the task and "
            "job acceptance, and call review_submit with PASS, CHANGES or REJECT and concrete evidence. "
            "BLOCKED means you could not complete the review because a tool, permission, gate runner or setup failed; the task waits for the user and the builder is never sent back. CHANGES and REJECT are only for defects you found in the code.")


def submit_as_standin(conn, project, task_id: int, head_sha: str, verdict: str, evidence: str) -> str:
    """The job's pinned coordinator reviews while the external manager is gone (F24).

    The supervisor spawns that coordinator only after the external lease lapses,
    e.g. when the manager chat stopped at a usage limit and did not wake. It may
    review only while the lease stays lapsed, only as the pinned manager model,
    only with no pending recovery barrier, and never its own work.
    """
    from . import processes
    from .manager_audit import authority
    if not enabled(project):
        raise PermissionError("external-manager review is not configured")
    if os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("a supervised worker cannot review")
    task = db.get_task(conn, task_id)
    if not task or not task.get("job_id"):
        raise ValueError("review requires a job task")
    try:
        process = processes.require_control(conn, purposes=("coordinator",), job_id=task["job_id"])
    except PermissionError as error:
        raise PermissionError(f"external-manager review: only the job's coordinator may stand in ({error})") from None
    lease = manager.lease(conn, task["job_id"])
    if lease and not lease["released_at"] and manager.fresh(lease) and manager.pid_alive(lease["pid"]):
        raise PermissionError("external-manager review stays with the attached external manager")
    try:
        authority(conn, project.root, task["job_id"])  # the coordinator runs the pinned manager model
    except PermissionError as error:
        raise PermissionError(f"external-manager review: {error}") from None
    if manager_state.pending(conn, task["job_id"]):
        raise PermissionError("manager recovery audit must be acknowledged before review")
    reviews.ensure_independent(conn, task, process)
    reviews.approve(conn, project, task_id, head_sha, verdict.upper(),
                    f"coordinator-standin:process-{process['id']}", evidence)
    db.log_event(conn, task_id, "external_manager_standin", cause=f"coordinator process {process['id']}",
                 detail={"head": head_sha, "verdict": verdict.upper()})
    return "Review recorded by the pinned coordinator standing in for the absent external manager."
