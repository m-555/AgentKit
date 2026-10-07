"""Record manager inspection of prevented attempts, bound to exact clean bytes."""
from __future__ import annotations

from . import audit, db, repo


def reviewed(conn, task, violations):
    if not violations:
        return True
    if not task.get("worktree") or not repo.is_clean(task["worktree"]):
        return False
    head = repo.head_commit(task["worktree"])
    required = {item["id"] for item in violations}
    for row in db.recent_events(conn, task_id=task["id"], kind="guard_denials_reviewed", limit=100):
        detail = row["detail"]
        if (detail.get("head") == head and type(detail.get("generation")) is int and detail["generation"] <= task["generation"]
                and required <= set(detail.get("violation_ids", []))):
            return True
    return False


def resolve(conn, project, task_id, head, evidence):
    from .host_completion import authority
    task = db.get_task(conn, task_id)
    if not task or task["status"] not in ("REVIEW", "BLOCKED", "DONE") or not evidence.strip():
        raise ValueError("guard review requires stopped preserved work and concrete evidence")
    holder = authority(conn, project, task)
    if repo.head_commit(task["worktree"]) != head or not repo.is_clean(task["worktree"]):
        raise ValueError("guard review must bind to the current clean commit")
    if task["status"] == "DONE":
        from .reviews import require_approval
        if task.get("last_commit") != head or require_approval(conn, task, project) != head:
            raise ValueError("completed denial review requires the exact approved recorded commit")
    result = audit.audit_worktree(conn, project, task["worktree"], task_id, record=False)
    if not result.clean:
        raise PermissionError(result.summary())
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (task_id,))]
    # L3/L4 guards prevent tool execution. Post-write/index/branch audit failures
    # cannot be dismissed by this entrypoint; they require quarantine and repair.
    if not rows or any(r["layer"] not in ("L3", "L4") or r["channel"] not in ("shell", "write", "pre_tool_use") for r in rows):
        raise PermissionError("only prevented pre-execution guard attempts may be reviewed")
    db.log_event(conn, task_id, "guard_denials_reviewed", cause=evidence,
                 detail={"head": head, "generation": task["generation"], "holder": holder,
                         "violation_ids": [r["id"] for r in rows]})
