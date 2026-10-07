"""Operator re-pin of a job's manager to another provider or model.

A job's coordinator pin never changes on its own, so autonomous coordinators
cannot drift between providers. When the user deliberately appoints a different
manager, for example because the pinned account is out for days, the operator
records that decision here.

Requests, decisions, tasks, failures and reviews are preserved. The previous
manager's lease is retired, so its credential stops working, and a fresh
recovery epoch opens: the new manager must attach, audit and acknowledge it
before integration or newly dependent launches resume.
"""
from __future__ import annotations

import json
import os

from . import db, jobs, manager, manager_state, models, processes
from .config import load_project
from .hook_worktree_paths import supervised
from .locking import atomic_write, exclusive


def repin(conn, root, job_id: str, profile_name: str, evidence: str, *, effort: str | None = None) -> dict:
    """Move `job_id` to the control profile `profile_name`; returns the new epoch."""
    if supervised() or os.environ.get("AGENTKIT_TASK") or os.environ.get("AGENTKIT_PROCESS"):
        raise PermissionError("a supervised worker or coordinator cannot re-pin a job's manager")
    if not evidence.strip() or len(evidence) > 2000:
        raise ValueError("re-pin needs 1-2000 characters of evidence for the user's decision")
    project = load_project(root)
    selection = models.profile(project, profile_name, control=True)
    if effort:
        selection = models.Profile(selection.name, selection.provider, selection.model, effort, selection.rank)
    with exclusive(root, "jobs"), db.immediate_transaction(conn):
        job = jobs.load(root, job_id)
        previous = job.get("coordinator_model")
        if previous == selection.to_dict():
            raise ValueError(f"job {job_id} is already pinned to {selection.provider}/{selection.model}")
        record = manager.lease(conn, job_id)
        if record and not record["released_at"] and manager.fresh(record):
            raise PermissionError(f"manager {record['holder']!r} still holds a fresh lease; "
                                  "release it or wait for it to expire")
        if any(p["purpose"] == "coordinator" and p["job_id"] == job_id for p in processes.owning(conn)):
            raise PermissionError("a coordinator process still owns this job; stop it first")
        retired = {k: v for k, v in (record or {}).items() if k != "token_hash"} or None
        # A retired lease row would keep fencing the job while its old bridge PID
        # (or an unrelated process reusing it) is alive, so it is removed; the
        # event below keeps its history.
        conn.execute("DELETE FROM manager_leases WHERE job_id=?", (job_id,))
        epoch = manager_state.record_outage(
            conn, job_id, selection.provider,
            f"manager re-pinned by operator to {selection.provider}/{selection.model}", new=True)
        db.log_event(conn, None, "manager_repinned", cause=evidence,
                     effect="new manager must attach, audit and acknowledge the fresh epoch",
                     detail={"job": job_id, "from": previous, "to": selection.to_dict(),
                             "epoch": epoch, "previous_lease": retired})
        job["coordinator"] = selection.provider
        job["coordinator_model"] = selection.to_dict()
        job["decisions"].append({"text": (f"Operator re-pinned the manager from "
                                          f"{(previous or {}).get('provider')}/{(previous or {}).get('model')} to "
                                          f"{selection.provider}/{selection.model}: {evidence}"),
                                 "at": db.utcnow()})
        # Written last, inside the transaction: a failed write rolls the database back.
        atomic_write(jobs.path(root, job_id), json.dumps(job, indent=2) + "\n")
    return {"job": job_id, "pinned": selection.to_dict(), "epoch": epoch}
