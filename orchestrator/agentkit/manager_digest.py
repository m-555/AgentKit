"""What a resumed manager reads: only the events that need its judgment.

A manager that resumes its own session already knows the plan. Re-reading the whole
job brief on every wake cost 22-48k characters per wake on the benchmark; this
digest names what changed instead, and job_brief stays one call away.
"""
from __future__ import annotations

from collections import Counter

from . import db, quota, workflow
from . import statemachine as sm

#: Upper bound on the digest, in characters.
LIMIT = 3000
ATTENTION = (sm.FAILED, sm.BLOCKED, sm.NEEDS_REPLAN, sm.STALE)


def _one_line(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def digest(conn, project, job) -> str:
    tasks = [task for task in db.list_tasks(conn) if task.get("job_id") == job["id"]]
    runtime = conn.execute("SELECT planned_revision FROM jobs WHERE id=?", (job["id"],)).fetchone()
    counts = Counter(task["status"] for task in tasks)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "no tasks yet"
    lines = [f"Job {job['id']}, revision {job['revision']}: {summary}."]
    if runtime and runtime["planned_revision"] != job["revision"]:
        lines.append(f"- The user's request changed: plan revision {job['revision']} (job_brief has the new request).")
    for task in tasks:
        waiting = task["status"] in ATTENTION and not quota.is_quota_paused(task)
        if waiting or (task["status"] in (sm.READY, sm.REVIEW) and task.get("blocker")):
            reason = _one_line(task.get("blocker") or task.get("next_action"))
            lines.append(f"- Task {task['id']} {task['status']} after {task['attempts']} attempts "
                         f"({_one_line(task['title'], 80)}): {reason}")
    ids = {task["id"] for task in tasks}
    for item in db.list_amendments(conn):
        if item.get("task_id") in ids:
            lines.append(f"- Open amendment {item['id']} on task {item['task_id']}: {_one_line(item['proposal'], 200)}")
    if tasks and all(task["status"] in (sm.DONE, sm.CANCELLED) for task in tasks):
        lines.append("- Every task is DONE or CANCELLED: define follow-up tasks for what is still missing; code "
                     "accepts the finished job." if workflow.enabled(project) else
                     "- Every task is DONE or CANCELLED: review the combined result against the job's acceptance "
                     "criteria and call job_accept, or define follow-up tasks.")
    text = "\n".join(lines)
    return text if len(text) <= LIMIT else text[:LIMIT - 45].rstrip() + "\n- (more in task_list)"


def wake_prompt(conn, project, job) -> str:
    return ("Continue as this job's manager in this same session; you already know its plan. "
            "Since your last turn:\n" + digest(conn, project, job) + "\n"
            + ("Act only on these items: replan a changed request (task_define, job_decision, job_plan_ready) "
               "and requeue, redefine or cancel a stopped task the user sent back to you (task_requeue). Review, "
               "acceptance and other stops run without you. "
               if workflow.enabled(project) else
               "Act only on these items: repair or requeue failed work (task_requeue), resolve blockers, replan a "
               "changed request (task_define, job_decision, job_plan_ready), or accept a finished job (job_accept). ")
            + "Review findings return to their builder automatically and need no action. Call job_brief only if "
            "this summary is not enough. Never replace the coordinator provider.")
