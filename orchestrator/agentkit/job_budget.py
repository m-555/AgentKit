"""An optional token budget per job: past it the job holds, with its work kept.

`budgets.job_tokens` in .ai/project.yaml caps the recorded tokens (input including
cache, plus output) of every session a job runs: manager, workers, reviewers and
retries. Over it, the job stops launching anything; sessions already running finish.
Raising or removing the budget lets it continue. A budget hold never claims the job
is done. Without the setting nothing changes.
"""
from __future__ import annotations

from . import db
from .job_usage import job_total

PREFIX = "budget: "


def limit(project) -> int | None:
    value = (project.raw.get("budgets") or {}).get("job_tokens")
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError("budgets.job_tokens must be a positive whole number of tokens")
    return value


def enforce(conn, project, job_id: str, runtime: dict) -> bool:
    """True while the job is held for its budget; resumes a held job once the budget allows."""
    if runtime["status"] == "DONE":
        return False
    cap = limit(project)
    held = runtime["status"] == "BLOCKED" and str(runtime.get("last_error") or "").startswith(PREFIX)
    if cap is None and not held:
        return False
    used = job_total(conn, project.root, job_id)
    if cap is not None and used is not None and used >= cap:
        if runtime["status"] != "BLOCKED":
            conn.execute("UPDATE jobs SET status='BLOCKED',last_error=?,updated_at=? WHERE id=?",
                         (f"{PREFIX}job used {used:,} tokens of its {cap:,} budget. Raise or remove "
                          "budgets.job_tokens to continue; work so far is kept.", db.utcnow(), job_id))
            db.log_event(conn, None, "job_budget_hold", cause=f"{used:,} of {cap:,} tokens",
                         effect="no new sessions until the budget is raised", detail={"job": job_id})
        return True
    if held:
        status = "ACTIVE" if runtime["planned_revision"] == runtime["revision"] else "PLANNING"
        conn.execute("UPDATE jobs SET status=?,last_error=NULL,updated_at=? WHERE id=? AND status='BLOCKED' "
                     "AND last_error LIKE ?", (status, db.utcnow(), job_id, PREFIX + "%"))
        db.log_event(conn, None, "job_budget_resumed", cause=f"{used or 0:,} tokens used; budget {cap or 'removed'}",
                     detail={"job": job_id, "status": status})
    return False
