"""Problems: a stopped task waits for you while the rest of its job keeps going.

The assigner plans a job once, and again only when you change the job. Nothing
wakes a model after that to decide what a stop means: a task that used up its
retries, a review that still asks for changes or rejects the approach, a tool or
setup failure, a failed merge, a builder asking for files it does not own, or a
question the assigner asked. Code writes down what happened and which replies can
fix it; the view lists it and, when set up, it is sent to your phone. Nothing
spends tokens while a problem waits, and tasks that do not depend on the stopped
one keep running and merging.

Replies, cheapest first:

* retry:   run the stopped step again as it is, after you fixed a tool, login or setup;
* accept:  take the builder's reviewed commit as it is (its gate still has to pass);
* skip:    cancel the task and every task that depends on it;
* dismiss: close a builder's request for more files;
* hint:    the same builder continues with your words as its current instruction;
* plan:    the assigner re-plans the job with your words; the only reply that starts a planner.

The same tool or setup problem in two tasks of one job is the environment, not the
tasks, so that job starts nothing new until you retry one of them.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from typing import Any

from . import db, infra_stop, manager_state, processes, quota, repo, test_packets, workflow
from . import statemachine as sm
from .control_prompts import REVIEW_REFUSED
from .job_acceptance import ACCEPT, FINAL

ACTIONS = ("retry", "accept", "skip", "dismiss", "hint", "plan")
TITLES = {
    "setup": "stopped on a tool or setup problem",
    "limit": "reached its run limit",
    "blocked": "is blocked",
    "failed": "failed and used up its automatic retries",
    "review": "was not approved by its review",
    "merge": "was approved but could not merge",
    "replan": "says it cannot be done as planned",
    "stale": "lost its session; its work is kept",
    "staging": "cannot pass the checks that let it merge",
    "scope": "asks for files it does not own",
    "question": "has a question for you",
    "budget": "used its token budget",
    "accept": "could not be accepted",
    "final": "was not passed by its final review",
    "launch": "could not start its review",
}
OPTIONS = {
    "setup": ("retry", "hint", "skip", "plan"),
    "limit": ("retry", "hint", "skip", "plan"),
    "blocked": ("retry", "hint", "skip", "plan"),
    "failed": ("retry", "hint", "skip", "plan"),
    "review": ("accept", "hint", "skip", "plan"),
    "merge": ("retry", "hint", "skip", "plan"),
    "replan": ("plan", "hint", "skip"),
    "stale": ("retry", "skip", "plan"),
    "staging": ("retry", "skip", "plan"),
    "scope": ("plan", "dismiss"),
    "question": ("plan",),
    "budget": (),
    "accept": ("retry", "plan"),
    "final": ("accept", "retry", "plan"),
    "launch": ("retry", "skip", "plan"),
}
#: A job held because the same setup problem stopped two of its tasks.
HOLD = "problems: "
#: Job stops that are not a question for the user.
_NOT_QUESTIONS = (HOLD, infra_stop.PREFIX, "budget: ", "paused: ", "preview:", ACCEPT, FINAL)
_CODE = "ACDEFGHJKMNPQRTUVWXY34679"
LIMIT = 600


def _clip(text: Any, limit: int = LIMIT) -> str:
    return " ".join(str(db.redact(str(text or ""))).split())[:limit]


def _latest_review(conn, task_id: int):
    return conn.execute("SELECT head_sha,verdict,evidence FROM reviews WHERE task_id=? AND verdict!='INVALIDATED' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def _head(task) -> str | None:
    try:
        return repo.head_commit(task["worktree"]) if task.get("worktree") else None
    except Exception:
        return None


def classify(conn, project, task) -> str | None:
    """The problem a task that is not running stands for, or None."""
    status = task["status"]
    blocker = str(task.get("blocker") or "")
    if status == sm.BLOCKED:
        if quota.is_quota_paused(task):
            return None  # resumes by itself when the provider is back
        if infra_stop.meta(task):
            return "setup"
        if not workflow.enabled(project):
            return None
        return "limit" if blocker.startswith("[AgentKit execution limit]") else "blocked"
    if not workflow.enabled(project):
        return None  # without a workflow the manager still owns these
    if status == sm.FAILED:
        review = _latest_review(conn, task["id"])
        if review and review["verdict"] == "PASS":
            return "merge"  # failed after approval: merge, contract or combined checks
        if review and review["verdict"] in ("CHANGES", "REJECT") and _head(task) == review["head_sha"]:
            return "review"
        return "failed"
    if status == sm.NEEDS_REPLAN:
        return "replan"
    if status == sm.STALE:
        return "stale"
    if status == sm.REVIEW and blocker.startswith(REVIEW_REFUSED):
        return "launch"
    if status == sm.REVIEW and blocker:
        from .review_policy import mode
        return "staging" if mode(project) != "ai" else None  # approval by staging or by its gate failed
    return None


def retries_itself(conn, project, task) -> bool:
    """Whether a FAILED task with retries left goes back to its builder without anyone deciding."""
    if not workflow.enabled(project):
        return True  # unchanged: the scheduler's own bounded retries
    if classify(conn, project, task) in ("review", "merge"):
        return False  # a rejected approach or a failed merge needs you
    # A separate tester whose tests fail cannot tell whether its tests or the code are wrong.
    return workflow.feature(project) or not test_packets.is_tester(project, task)


def _options(conn, kind: str, task) -> list[str]:
    options = list(OPTIONS[kind])
    if kind == "setup" and infra_stop.meta(task).get("stage") == "review":
        options.remove("hint")  # the reviewer could not run: the builder has nothing to change
    if kind == "merge" and not str(task.get("blocker") or "").startswith(("combined_gate:", "contract:")):
        options.remove("retry")  # only combined checks can rerun unchanged
    return options


def _task_item(conn, project, task, kind: str) -> dict:
    blocker = str(task.get("blocker") or "")
    detail = _clip(blocker or task.get("next_action") or "no detail recorded")
    if kind == "review":
        review = _latest_review(conn, task["id"])
        detail = _clip(f"{review['verdict']}: {review['evidence']}")
    digest = hashlib.sha256(blocker.encode()).hexdigest()[:12]
    return {"job_id": task["job_id"], "task_id": task["id"], "kind": kind,
            "signature": f"task:{task['id']}:{kind}:{task.get('generation')}:{task.get('attempts')}:{digest}",
            "title": f"Task {task['id']} {TITLES[kind]}: {_clip(task.get('title'), 120)}",
            "detail": detail, "options": _options(conn, kind, task)}


def _job_item(job: dict, kind: str, detail: str, signature: str) -> dict:
    return {"job_id": job["id"], "task_id": None, "kind": kind, "signature": signature,
            "title": f"Job {job['id']} {TITLES[kind]}", "detail": _clip(detail), "options": list(OPTIONS[kind])}


def current(conn, project) -> dict[str, dict]:
    """Every problem that stands now, keyed by what makes it the same problem."""
    found: dict[str, dict] = {}
    busy = {p["task_id"] for p in processes.owning(conn)}
    tasks: dict[str, list[dict]] = {}
    for task in db.list_tasks(conn):
        if task.get("job_id"):
            tasks.setdefault(task["job_id"], []).append(task)
    amendments = db.list_amendments(conn)
    for row in conn.execute("SELECT * FROM jobs WHERE status!='DONE'").fetchall():
        job = dict(row)
        if manager_state.pending(conn, job["id"]):
            continue  # an outage recovery owns this job until its audit is acknowledged
        own = tasks.get(job["id"], [])
        for task in own:
            kind = None if task["id"] in busy else classify(conn, project, task)
            if kind:
                item = _task_item(conn, project, task, kind)
                found[item["signature"]] = item
        error = str(job.get("last_error") or "")
        if job["status"] == "BLOCKED" and error and workflow.enabled(project):
            digest = hashlib.sha256(error.encode()).hexdigest()[:12]
            if error.startswith("budget: "):
                item = _job_item(job, "budget", error, f"job:{job['id']}:budget")
                found[item["signature"]] = item
            elif error.startswith((ACCEPT, FINAL)):
                kind = "accept" if error.startswith(ACCEPT) else "final"
                item = _job_item(job, kind, error.split(": ", 1)[1], f"job:{job['id']}:{kind}:{digest}")
                found[item["signature"]] = item
            elif not error.startswith(_NOT_QUESTIONS):
                item = _job_item(job, "question", error, f"job:{job['id']}:question:{digest}")
                found[item["signature"]] = item
        if workflow.enabled(project):
            ids = {t["id"] for t in own}
            for amendment in amendments:
                if amendment.get("task_id") in ids:
                    item = {"job_id": job["id"], "task_id": amendment["task_id"], "kind": "scope",
                            "signature": f"amendment:{amendment['id']}",
                            "title": f"Task {amendment['task_id']} {TITLES['scope']}",
                            "detail": _clip(f"{amendment['proposal']}. Why: {amendment.get('rationale') or 'not given'}"),
                            "options": list(OPTIONS["scope"])}
                    found[item["signature"]] = item
    return found


def _new_code(conn) -> str:
    while True:
        code = "".join(secrets.choice(_CODE) for _ in range(6))
        if not conn.execute("SELECT 1 FROM problems WHERE code=?", (code,)).fetchone():
            return code


def _row(conn, problem_id: int) -> dict:
    row = dict(conn.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone())
    row["options"] = json.loads(row["options"])
    return row


def sync(conn, project) -> list[dict]:
    """Bring the problem list up to date; returns the problems this call opened."""
    found = current(conn, project)
    known = {r["signature"]: dict(r) for r in conn.execute("SELECT * FROM problems WHERE status IN ('OPEN','WAITING')")}
    opened = []
    for signature, item in found.items():
        row = known.get(signature)
        if row is None:
            if conn.execute("SELECT 1 FROM problems WHERE signature=?", (signature,)).fetchone():
                continue  # answered already; only a new stop is a new problem
            cursor = conn.execute(
                "INSERT INTO problems(job_id,task_id,kind,signature,code,title,detail,options,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,'OPEN',?)",
                (item["job_id"], item["task_id"], item["kind"], signature, _new_code(conn), item["title"],
                 item["detail"], json.dumps(item["options"]), db.utcnow()))
            opened.append(_row(conn, cursor.lastrowid))
            db.log_event(conn, item["task_id"], "problem_opened", cause=item["detail"],
                         effect="waits for the user; the rest of the job continues",
                         detail={"job": item["job_id"], "kind": item["kind"]})
        elif row["status"] == "WAITING" and _replanned(conn, row):
            conn.execute("UPDATE problems SET status='OPEN',notified_at=NULL,detail=? WHERE id=?",
                         (_clip("The assigner re-planned, but this is still stopped. " + item["detail"]), row["id"]))
            opened.append(_row(conn, row["id"]))
    for signature, row in known.items():
        if signature not in found:
            conn.execute("UPDATE problems SET status='RESOLVED',resolved_at=?,resolution=COALESCE(resolution,'cleared') "
                         "WHERE id=?", (db.utcnow(), row["id"]))
    _hold_jobs(conn)
    return opened


def _replanned(conn, row) -> bool:
    job = conn.execute("SELECT status,planned_revision FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
    return bool(job and job["status"] == "ACTIVE" and int(job["planned_revision"] or 0) >= int(row["wait_revision"] or 0))


def _hold_jobs(conn) -> None:
    """Hold a job whose tasks hit the same setup problem twice; release it when that clears."""
    stops: dict[str, list[tuple[int, str]]] = {}
    for task in infra_stop.stopped(conn):
        stage = infra_stop.meta(task).get("stage")
        if stage in ("review", "worker") and task.get("job_id"):
            stops.setdefault(task["job_id"], []).append((task["id"], stage))
    for row in conn.execute("SELECT id,status,last_error,revision,planned_revision FROM jobs WHERE status IN ('ACTIVE','BLOCKED')"):
        counts = Counter(stage for _, stage in stops.get(row["id"], []))
        stage, count = counts.most_common(1)[0] if counts else (None, 0)
        held = row["status"] == "BLOCKED" and str(row["last_error"] or "").startswith(HOLD)
        if count >= 2 and row["status"] == "ACTIVE":
            ids = ", ".join(str(task) for task, at in stops[row["id"]] if at == stage)
            conn.execute("UPDATE jobs SET status='BLOCKED',last_error=?,updated_at=? WHERE id=?",
                         (f"{HOLD}the same {stage} setup problem stopped tasks {ids}; nothing new starts in this "
                          "job until you retry one of them", db.utcnow(), row["id"]))
            db.log_event(conn, None, "job_held_for_setup", cause=f"{stage} problem in tasks {ids}",
                         effect="no new sessions in this job", detail={"job": row["id"]})
        elif held and count < 2:
            status = "ACTIVE" if row["planned_revision"] == row["revision"] else "PLANNING"
            conn.execute("UPDATE jobs SET status=?,last_error=NULL,updated_at=? WHERE id=?",
                         (status, db.utcnow(), row["id"]))
            db.log_event(conn, None, "job_hold_released", detail={"job": row["id"], "status": status})


def listing(conn, job_id: str | None = None) -> list[dict]:
    """Open problems, newest first, with their task's title and state."""
    try:
        rows = conn.execute("SELECT * FROM problems WHERE status IN ('OPEN','WAITING') ORDER BY id DESC").fetchall()
    except Exception:
        return []  # a database from before problems existed has none
    out = []
    for row in rows:
        item = dict(row)
        item["options"] = json.loads(item["options"])
        task = db.get_task(conn, item["task_id"]) if item["task_id"] else None
        item["task_title"] = task.get("title") if task else None
        item["task_status"] = task.get("status") if task else None
        if job_id is None or item["job_id"] == job_id:
            out.append(item)
    return out


def find(conn, ref) -> dict | None:
    """A problem by id or by its code, in any letter case."""
    text = str(ref).strip()
    if text.isdecimal():
        row = conn.execute("SELECT * FROM problems WHERE id=?", (int(text),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM problems WHERE code=?", (text.upper(),)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["options"] = json.loads(item["options"])
    return item


def act(root, ref, action: str, text: str = "", *, by: str = "view") -> str:
    from .config import load_project
    conn = db.connect(root)
    try:
        return act_locked(conn, load_project(root), ref, action, text, by=by)
    finally:
        conn.close()


def _choices(options) -> str:
    return options[0] if len(options) == 1 else ", ".join(options[:-1]) + " or " + options[-1]


def act_locked(conn, project, ref, action: str, text: str = "", *, by: str = "view") -> str:
    """Answer one open problem; returns what happened, in one sentence."""
    from . import problem_actions
    row = find(conn, ref)
    if row is None or row["status"] != "OPEN":
        raise ValueError(f"problem {ref} is not open; it was answered already or no longer applies")
    action = {"answer": "plan"}.get(action.strip().lower(), action.strip().lower())
    if action not in row["options"]:
        raise ValueError(f"{action or 'that'} is not a reply to this problem; choose {_choices(row['options'])}"
                         if row["options"] else "this problem only informs you; change it in the view")
    words = str(text or "").strip()[:2000]
    if action in ("hint", "plan") and not words and row["kind"] != "final":  # findings are the words
        raise ValueError("write the words the " + ("builder" if action == "hint" else "assigner") + " should get")
    message = problem_actions.run(conn, project, row, action, words, by)
    if action == "plan":
        revision = int(conn.execute("SELECT revision FROM jobs WHERE id=?", (row["job_id"],)).fetchone()[0])
        conn.execute("UPDATE problems SET status='WAITING',wait_revision=?,resolution=? WHERE id=?",
                     (revision, f"plan by {by}", row["id"]))
    else:
        conn.execute("UPDATE problems SET status='RESOLVED',resolved_at=?,resolution=? WHERE id=?",
                     (db.utcnow(), f"{action} by {by}", row["id"]))
    db.log_event(conn, row["task_id"], "problem_answered", cause=f"{action} by {by}", effect=message,
                 detail={"problem": row["code"], "job": row["job_id"], "words": words[:500]})
    return message
