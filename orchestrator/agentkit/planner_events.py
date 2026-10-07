"""Event-driven planner dispatch; unchanged blockers do not burn another turn."""
from __future__ import annotations

import hashlib
import json

from . import adapters, db, manager_state, processes, providers, workflow


def fingerprint(conn, project, job, tasks):
    ids = {task["id"] for task in tasks}
    amendments = [item["id"] for item in db.list_amendments(conn) if item.get("task_id") in ids]
    state = manager_state.state(conn, job["id"])
    values = {
        "revision": job["revision"], "mode": project.raw.get("workflow"),
        "epoch": [state.get("epoch"), state.get("acknowledged_epoch")],
        "tasks": [{key: task.get(key) for key in
                   ("id", "spec_hash", "status", "generation", "blocker", "next_action", "attempts")}
                  for task in sorted(tasks, key=lambda item: item["id"])],
        "amendments": sorted(amendments),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def should_dispatch(conn, project, job, tasks):
    if not workflow.enabled(project):
        return True
    value = fingerprint(conn, project, job, tasks)
    row = conn.execute("SELECT * FROM planner_events WHERE job_id=?", (job["id"],)).fetchone()
    if not row or row["fingerprint"] != value:
        return True
    process = processes.get(conn, row["process_id"])
    if not process:
        return False  # Lost evidence requires operator inspection, not guessed dispatch.
    state = providers.get_state(conn, process["provider"], process.get("account") or "default")
    adapter = adapters.get(process["provider"])
    classification = adapter.classify_error(process.get("error") or "", process.get("exit_code") or 1) if adapter else None
    return (process["status"] == "FAILED" and classification and classification.is_provider_problem
            and state.status == providers.AVAILABLE and state.detected_at != row["provider_observed_at"])


def record(conn, project, job, tasks, identifier):
    if not workflow.enabled(project) or not identifier:
        return
    process = processes.get(conn, identifier)
    observed = None
    if process:
        observed = providers.get_state(conn, process["provider"], process.get("account") or "default").detected_at
    conn.execute("INSERT INTO planner_events(job_id,fingerprint,process_id,provider_observed_at) "
                 "VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET fingerprint=excluded.fingerprint,"
                 "process_id=excluded.process_id,provider_observed_at=excluded.provider_observed_at",
                 (job["id"], fingerprint(conn, project, job, tasks), identifier, observed))
