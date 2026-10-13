"""What each reply to a problem does. Only `plan` starts a model that was not already working."""
from __future__ import annotations

from . import db, infra_stop, jobs, processes
from . import statemachine as sm


def run(conn, project, row: dict, action: str, words: str, by: str) -> str:
    return {"retry": _retry, "accept": _accept, "skip": _skip, "dismiss": _dismiss,
            "hint": _hint, "plan": _plan}[action](conn, project, row, words, by)


def _task(conn, row):
    task = db.get_task(conn, row["task_id"]) if row["task_id"] else None
    if task is None:
        raise ValueError("this problem has no task to act on")
    return task


def _reopen(conn, job_id: str) -> None:
    conn.execute("UPDATE jobs SET status='ACTIVE',last_error=NULL,updated_at=? WHERE id=? AND status='BLOCKED'",
                 (db.utcnow(), job_id))


def _retry(conn, project, row, words, by):
    if row["kind"] in ("accept", "final"):
        if row["kind"] == "final":
            from .job_acceptance import user_verdict
            user_verdict(conn, project, row["job_id"], "RETRY", by)
        _reopen(conn, row["job_id"])
        return f"Job {row['job_id']}: " + ("a new final review starts." if row["kind"] == "final" else
                                           "acceptance runs again.")
    task = _task(conn, row)
    if row["kind"] in ("staging", "launch"):
        db.update_task(conn, task["id"], blocker=None)
        return f"Task {task['id']}: its checks run again."
    if row["kind"] == "setup":
        return infra_stop.retry(conn, project, task["id"])
    if row["kind"] == "merge":
        from .integration_retry import authorize
        authorize(conn, project, task["id"], f"The user ({by}) asked to rerun the combined checks unchanged.")
        return f"Task {task['id']}: its approved commit reruns the combined checks; no model starts."
    from .requeue import requeue
    requeue(conn, project.root, task["id"], "Retry requested by the user. Last stop: " +
            str(task.get("blocker") or task.get("next_action") or "not recorded")[:1500],
            reset_attempts=False, cause=f"user retried ({by})")
    return f"Task {task['id']} runs again from its preserved work."


def _hint(conn, project, row, words, by):
    task = _task(conn, row)
    instruction = "The user says: " + words
    if row["kind"] == "review":
        instruction += "\nThe review said: " + row["detail"]
    from .requeue import requeue
    requeue(conn, project.root, task["id"], instruction[:2000], reset_attempts=False, cause=f"user hint ({by})")
    return f"Task {task['id']}: its builder continues with your words."


def _accept(conn, project, row, words, by):
    if row["kind"] == "final":
        from .job_acceptance import user_verdict
        user_verdict(conn, project, row["job_id"], "PASS", by)
        _reopen(conn, row["job_id"])
        return f"Job {row['job_id']}: you accepted the combined result; it is accepted once its checks pass."
    from .reviews import user_accept
    task = _task(conn, row)
    head = user_accept(conn, project, task["id"], by=by,
                       evidence="The user accepted this commit over the review: " + row["detail"])
    return f"Task {task['id']}: you accepted commit {head[:12]}; it merges next."


def dependents(conn, task) -> list[dict]:
    """Every unfinished task of the job that waits on this one, directly or through another."""
    found: dict[int, dict] = {}
    names = {str(task["id"]), *([str(task["spec_id"])] if task.get("spec_id") else [])}
    changed = True
    while changed:
        changed = False
        for other in db.list_tasks(conn):
            if (other.get("job_id") != task.get("job_id") or other["id"] in found or other["id"] == task["id"]
                    or other["status"] in sm.TERMINAL):
                continue
            if names & {str(dep) for dep in other["depends_on"]}:
                found[other["id"]] = other
                names |= {str(other["id"]), *([str(other["spec_id"])] if other.get("spec_id") else [])}
                changed = True
    return list(found.values())


def _skip(conn, project, row, words, by):
    task = _task(conn, row)
    chain = [task, *dependents(conn, task)]
    busy = {p["task_id"] for p in processes.owning(conn)}
    for item in chain:
        if item["id"] in busy:
            raise ValueError(f"task {item['id']} is running; stop it before skipping")
        if not sm.can(item["status"], sm.CANCELLED, "human"):
            raise ValueError(f"task {item['id']} is {item['status']} and cannot be cancelled now")
    for item in chain:
        db.release_leases(conn, item["id"], reason=f"skipped by the user ({by})")
        db.set_status(conn, item["id"], sm.CANCELLED, actor="human",
                      cause=f"user skipped task {task['id']} ({by})")
    rest = [str(item["id"]) for item in chain[1:]]
    return (f"Task {task['id']} cancelled" + (f", with the tasks that depend on it: {', '.join(rest)}" if rest else "")
            + ". The rest of the job continues.")


def _dismiss(conn, project, row, words, by):
    amendment = int(row["signature"].split(":", 1)[1])
    db.resolve_amendment(conn, amendment, "RESOLVED")
    db.log_event(conn, row["task_id"], "amendment_resolved", cause=f"dismissed by the user ({by})",
                 detail={"amendment": amendment})
    return f"Request {amendment} dismissed; the task keeps its scope."


def _plan(conn, project, row, words, by):
    about = f"\n(Reply to problem {row['code']}: {row['title']})"
    text = words or "Fix what the final review found: " + row["detail"]
    job = jobs.amend(project.root, row["job_id"], text + about, user=True)
    return f"Job {row['job_id']} goes back to the assigner with your words (revision {job['revision']})."

