"""Transport/preflight failures describe holds without erasing repair instructions."""
from . import db


def record(conn, task_id, reason):
    current = db.get_task(conn, task_id) or {}
    db.update_task(conn, task_id, blocker=reason,
                   next_action=current.get("next_action") or reason)
