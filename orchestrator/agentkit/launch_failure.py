"""Transport/preflight failures describe holds without erasing repair instructions.

A refusal becomes the task's next action only when nothing else was planned.
Each new refusal is logged once, so a later successful launch can tell a next
action that merely repeats a resolved refusal from a manager's instruction and
clear only the former (F21): the next worker must not be told to stop for a
problem that no longer exists.
"""
from . import db


def record(conn, task_id, reason):
    current = db.get_task(conn, task_id) or {}
    if current.get("blocker") != reason:  # the supervisor retries every pass
        db.log_event(conn, task_id, "launch_refused", cause=reason[:200], detail={"reason": reason})
    db.update_task(conn, task_id, blocker=reason,
                   next_action=current.get("next_action") or reason)


def cleared(conn, task_id):
    """Fields a successful launch resets: a next action that only repeats a recorded refusal."""
    stale = (db.get_task(conn, task_id) or {}).get("next_action")
    if not stale:
        return {}
    refusals = db.recent_events(conn, task_id, kind="launch_refused", limit=100)
    if any((event.get("detail") or {}).get("reason") == stale for event in refusals):
        return {"next_action": None}
    return {}
