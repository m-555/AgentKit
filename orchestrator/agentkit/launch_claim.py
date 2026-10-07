"""Fence slow host preparation against concurrent replans before claiming a worker."""
from . import db, manager_state, processes


def claim(conn, task, reason):
    """Claim state and generation together; a stale preparation consumes neither."""
    with db.immediate_transaction(conn):
        current = db.get_task(conn, task["id"])
        fields = ("generation", "spec_hash", "expected_read", "expected_write",
                  "role", "model_assignment", "gate_level", "base_sha", "job_id")
        if (not current or current["status"] != "READY"
                or any(current.get(k) != task.get(k) for k in fields)
                or any(p["task_id"] == task["id"] for p in processes.owning(conn))):
            return None, "task changed during preparation; discard stale launch"
        job_id = current.get("job_id")
        if job_id and not conn.execute(
                "SELECT 1 FROM jobs WHERE id=? AND status='ACTIVE' AND revision=planned_revision",
                (job_id,)).fetchone():
            return None, "job changed during preparation; planning required"
        manager_state.capture_current(conn)
        if not manager_state.allows_launch(conn, current):
            return None, "manager recovery audit required after preparation"
        generation = db.bump_generation(conn, task["id"])
        db.set_status(conn, task["id"], "LEASED", actor="scheduler", cause=reason)
        return generation, ""


def release_failed(conn, task_id, generation, reason):
    """Release only the claim we own, preserving a concurrent hold or replan."""
    with db.immediate_transaction(conn):
        current = db.get_task(conn, task_id)
        if current and current["generation"] == generation and current["status"] == "LEASED":
            db.set_status(conn, task_id, "READY", actor="scheduler", cause=reason)
