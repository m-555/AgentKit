"""Infrastructure problems stop the task for the user; they are never a code verdict.

A denied tool, a guard that refuses a legitimate step or a broken setup cannot be
fixed by the builder or by the manager. Treated as review findings they turned
into a loop: the builder redid a correct commit, the same commit was reviewed and
rejected again, and the manager was woken in between each time (benchmark,
2026-10-11: 40% of all tokens). Three signals now stop the task instead:

* a reviewer answers BLOCKED: it could not complete the review;
* a worker reports BLOCKED with `infrastructure=True`;
* a builder resubmits the exact commit a review already rejected.

The task is held in BLOCKED with its preserved work and becomes a problem for the
user (problems.py); nothing starts for it until the user fixes the cause and
retries (`agentkit task retry`, a reply to the problem, or Retry in the view). The
rest of its job keeps going. The same problem in two tasks of a job holds that
job (problems._hold_jobs), because then the cause is the environment.
"""
from __future__ import annotations

import json
from typing import Any

from . import db, repo
from . import statemachine as sm

REASON = "infrastructure"
PREFIX = "infrastructure: "
STAGES = ("review", "worker", "unchanged")


def meta(task: dict[str, Any]) -> dict[str, Any]:
    """The infrastructure stop recorded on a task, or {}."""
    try:
        value = json.loads(task.get("blocked_meta") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("reason") == REASON else {}


def stop(conn, task_id: int, stage: str, evidence: str, *, head: str | None = None, actor: str = "scheduler") -> None:
    """Hold the task with its work until the user retries; the rest of its job continues."""
    if stage not in STAGES:
        raise ValueError(f"unknown infrastructure stage {stage!r}")
    task = db.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} does not exist")
    detail = " ".join(str(db.redact(evidence)).split())[:1000] or "no detail reported"
    what = {"review": "Review could not be completed",
            "worker": "Worker stopped on a tool or setup problem",
            "unchanged": "Builder resubmitted a commit review already rejected"}[stage]
    db.set_status(conn, task_id, sm.BLOCKED, actor=actor, cause=f"{what}: {detail}")
    record = {"reason": REASON, "stage": stage, "head": head, "at": db.utcnow()}
    db.update_task(conn, task_id, blocked_meta=json.dumps(record), blocker=f"{what}: {detail}",
                   next_action="User: fix the tool, permission or setup problem, then retry the task "
                               "(a reply to the problem, or Retry in the view). No session starts for it until then.")
    db.log_event(conn, task_id, "infrastructure_stop", cause=detail,
                 effect="task held until the user retries; the rest of its job continues",
                 detail={"stage": stage, "head": head, "job": task.get("job_id")})


def rejected_head(conn, task_id: int) -> dict[str, Any] | None:
    """The latest review, when it rejected or could not review its commit."""
    row = conn.execute("SELECT head_sha,verdict,evidence FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    return dict(row) if row and row["verdict"] in ("CHANGES", "BLOCKED") else None


def resubmitted(conn, task_id: int, head: str) -> bool:
    """Stop a builder that hands back the exact commit review already rejected."""
    previous = rejected_head(conn, task_id)
    if not previous or previous["head_sha"] != head:
        return False
    stop(conn, task_id, "unchanged",
         f"commit {head[:12]} is unchanged since review answered {previous['verdict']}: {previous['evidence']}",
         head=head)
    return True


def stopped(conn, job_id: str | None = None) -> list[dict[str, Any]]:
    tasks = [t for t in db.list_tasks(conn, (sm.BLOCKED,)) if meta(t)]
    return [t for t in tasks if job_id is None or t.get("job_id") == job_id]


def retry(conn, project, task_id: int) -> str:
    """Resume after the operator fixed the cause: the same review, or the same task."""
    task = db.get_task(conn, task_id)
    record = meta(task) if task else {}
    if not task or task["status"] != sm.BLOCKED or not record:
        raise ValueError(f"task {task_id} is not stopped on an infrastructure problem")
    work = task.get("worktree")
    same = bool(record.get("head") and work and repo.head_commit(work) == record["head"] and repo.is_clean(work))
    target = sm.REVIEW if record["stage"] in ("review", "unchanged") and same else sm.READY
    db.set_status(conn, task_id, target, actor="human", cause="operator retried after an infrastructure stop")
    db.update_task(conn, task_id, blocked_meta=None, blocker=None, next_action=None)
    job = task.get("job_id")
    resumed = False
    if job and not stopped(conn, job):
        # Jobs stopped by this module before stops became per task.
        resumed = conn.execute(
            "UPDATE jobs SET status='ACTIVE',last_error=NULL,updated_at=? WHERE id=? AND status='BLOCKED' "
            "AND last_error LIKE ?", (db.utcnow(), job, PREFIX + "%")).rowcount > 0
    db.log_event(conn, task_id, "infrastructure_retry", effect=f"task -> {target}",
                 detail={"job": job, "job_resumed": resumed})
    step = "its review of the same commit restarts" if target == sm.REVIEW else "it is ready to run again"
    rest = "; the job runs again" if resumed else ""
    return f"Task {task_id}: {step}{rest}."
