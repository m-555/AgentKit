"""A user-approved trial of a local, tool-confined writer on one HOTSPOT task (F35).

A local worker has no OS sandbox, so a HOTSPOT task normally needs a stronger
worker (F22). The user may still want to measure a local model on one. The
attached manager records that decision, with the user's words as evidence, for
that task only. The task stays HOTSPOT for leases, review and integration; the
local worker runs it under the bounded-writer confinement every local writer
gets: its declared files only, no command tool, host monitor and pre-commit
guards. Decouple and contract work are never eligible, because their changes
cross module contracts that tool confinement cannot bound. The approval names
the task's spec hash, so it lapses when the task is rewritten.
"""
from __future__ import annotations

import json

from . import db

ELIGIBLE = ("HOTSPOT",)
#: The requirements a trial is measured against: those of the bounded writer it runs as.
BOUNDED_KIND = "DEPENDENT"
_CLOSED = ("DONE", "CANCELLED", "INTEGRATION_READY", "INTEGRATING")


def _record(task) -> dict | None:
    raw = (task or {}).get("local_trial")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def granted(task) -> bool:
    """True when this task carries a current, evidenced trial approval."""
    value = _record(task)
    return bool(value and task.get("kind") in ELIGIBLE and str(value.get("evidence") or "").strip()
                and value.get("spec_hash") == (task.get("spec_hash") or ""))


def grant(conn, job_id: str, task_id: int, token: str, evidence: str) -> dict:
    """Record the user's decision; only the job's attached manager may."""
    from . import manager
    record = manager.require_lease(conn, job_id, token)
    task = db.get_task(conn, task_id)
    if not task or task.get("job_id") != job_id:
        raise PermissionError("the task belongs to another job")
    if task.get("kind") not in ELIGIBLE:
        raise ValueError(f"a local trial covers {', '.join(ELIGIBLE)} tasks only, not {task.get('kind')}")
    if task.get("status") in _CLOSED:
        raise ValueError(f"task {task_id} is {task['status']}; a trial applies to work not yet done")
    if not evidence.strip():
        raise ValueError("record the user's decision as evidence")
    value = {"granted_by": record["holder"], "granted_at": db.utcnow(), "evidence": evidence.strip(),
             "spec_hash": task.get("spec_hash") or ""}
    with db.immediate_transaction(conn):
        conn.execute("UPDATE tasks SET local_trial=? WHERE id=?", (json.dumps(value), task_id))
        db.log_event(conn, task_id, "local_trial_granted", detail={"job": job_id, **value})
    return value
