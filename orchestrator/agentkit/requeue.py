"""Return a stopped task to its builder with one instruction; its preserved work stays."""
from __future__ import annotations

from pathlib import Path

from . import db, processes


def requeue(conn, root: str | Path, task_id: int, instructions: str, *, reset_attempts: bool = True,
            cause: str = "coordinator authorized recovery") -> None:
    task = db.get_task(conn, task_id)
    if task is None:
        raise ValueError("task not found")
    if any(p["task_id"] == task_id for p in processes.owning(conn)):
        raise ValueError("previous session still owns this task; prove monitor and child stopped")
    from . import recovery_runtime
    held_ready = task["status"] == "READY" and recovery_runtime.pending(conn, task_id=task_id)
    if task["status"] not in ("FAILED", "STALE", "NEEDS_REPLAN", "BLOCKED") and not held_ready:
        raise ValueError("task does not need recovery")
    if not instructions.strip() or len(instructions) > 2000:
        raise ValueError("retry needs 1-2000 characters of planner instructions")
    recovery_runtime.supersede_worker_intents(conn, task_id, instructions)
    from .reconcile import apply_current_spec
    apply_current_spec(conn, root, task_id)  # a requeued task runs its current spec (F36)
    db.bump_generation(conn, task_id)
    fields: dict[str, object] = {"next_action": instructions, "blocker": None, "blocked_meta": None}
    if reset_attempts:
        fields["attempts"] = 0
    db.update_task(conn, task_id, **fields)
    db.set_status(conn, task_id, "READY", actor="scheduler", cause=cause)
