"""When the assigner runs: for a new or changed job, and not for what code or the user answers.

With a workflow, a planned job wakes its assigner only when the user changes it
(a new revision), when it has no tasks yet, for an outage recovery, an unfinished
human preview, or an external manager's waiting per-task reviews. Stops, failures,
review disagreements and scope requests are problems for the user (problems.py),
and code accepts a finished job (job_acceptance.py). Without a workflow the manager
keeps its old duties, except setup stops, which no model can fix.
"""
from __future__ import annotations

from . import db, infra_stop, manager_state, quota, workflow


def needs_plan(conn, project, job, runtime, tasks, review_mode: str, active_ids) -> bool:
    wake = manager_state.pending(conn, job["id"]) or runtime["planned_revision"] != job["revision"] or not tasks
    wake |= bool(runtime.get("last_error", "") and str(runtime["last_error"]).startswith("preview:"))
    from .external_review import enabled as external_reviews
    # Reviews of an absent external manager wait for the coordinator (F29).
    wake |= review_mode == "ai" and external_reviews(project) and any(
        t["status"] == "REVIEW" and t["id"] not in active_ids for t in tasks)
    if workflow.enabled(project):
        return wake
    wake |= review_mode != "human" and bool(tasks) and all(t["status"] in ("DONE", "CANCELLED") for t in tasks)
    wake |= any(t["status"] in ("NEEDS_REPLAN", "STALE") or (t["status"] == "FAILED" and t["attempts"] >= 3)
                for t in tasks)
    wake |= any((t["status"] == "BLOCKED" and not quota.is_quota_paused(t) and not infra_stop.meta(t)) or
                (t["status"] in ("READY", "REVIEW") and t.get("blocker")) for t in tasks)
    wake |= any(a.get("task_id") in {t["id"] for t in tasks} for a in db.list_amendments(conn))
    return wake
