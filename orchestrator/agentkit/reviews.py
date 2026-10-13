"""Commit-bound independent approvals. A verdict never approves later edits."""
from __future__ import annotations

import sqlite3

from . import audit, db, repo
from .config import ProjectConfig


def approve(conn: sqlite3.Connection, project: ProjectConfig, task_id: int,
            head: str, verdict: str, reviewer: str, evidence: str) -> None:
    task = db.get_task(conn, task_id)
    if not task or task["status"] != "REVIEW":
        raise ValueError("only a task in REVIEW may receive a verdict")
    if verdict not in ("PASS", "CHANGES", "REJECT", "BLOCKED") or not evidence.strip():
        raise ValueError("a verdict and concrete review evidence are required")
    worktree = task.get("worktree")
    if not worktree or repo.head_commit(worktree) != head or not repo.is_clean(worktree):
        raise ValueError("review must reference the current clean worker commit")
    if not audit.audit_worktree(conn, project, worktree, task_id, record=False).clean:
        raise ValueError("cannot approve an out-of-scope change")
    if verdict == "PASS":
        from .worker_preparation import validate_completed
        validate_completed(project, task, worktree)
        from .verification import run
        result = run(conn, project, worktree, task["gate_level"])
        if not result.passed or repo.head_commit(worktree) != head or not repo.is_clean(worktree):
            raise ValueError("review gate failed or changed the reviewed tree: " + result.summary())
        db.record_gate(conn, task_id, task["gate_level"], head, True, result.summary())
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (task_id, head, verdict, reviewer, str(db.redact(evidence)), db.utcnow()))
    if verdict == "BLOCKED":
        # The reviewer could not review: an infrastructure problem, never a code verdict.
        from . import infra_stop
        infra_stop.stop(conn, task_id, "review", evidence, head=head, actor="reviewer")
        return
    target = "INTEGRATION_READY" if verdict == "PASS" else "FAILED"
    db.set_status(conn, task_id, target, actor="reviewer", cause=evidence[:1000])
    db.log_event(conn, task_id, "review_approved" if verdict == "PASS" else "review_rejected",
                 cause=evidence, detail={"head": head, "reviewer": reviewer, "verdict": verdict})
    if verdict == "PASS":
        db.update_task(conn, task_id, blocker=None, next_action="Exact commit approved; automatic integration follows.")
    else:
        db.update_task(conn, task_id, blocker=evidence, next_action="Address review findings: " + evidence)


def user_accept(conn: sqlite3.Connection, project: ProjectConfig, task_id: int, *, by: str, evidence: str) -> str:
    """The user takes a commit its review did not approve; scope and gate still have to pass."""
    task = db.get_task(conn, task_id)
    if not task or task["status"] != "FAILED":
        raise ValueError("only a task whose review did not approve it can be accepted")
    last = conn.execute("SELECT head_sha,verdict FROM reviews WHERE task_id=? AND verdict!='INVALIDATED' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    worktree = task.get("worktree")
    if not worktree:
        raise ValueError("the task has no worktree to accept")
    head = repo.head_commit(worktree)
    if not last or last["verdict"] not in ("CHANGES", "REJECT") or last["head_sha"] != head or not repo.is_clean(worktree):
        raise ValueError("the reviewed commit changed since its review; retry or hint instead")
    if not audit.audit_worktree(conn, project, worktree, task_id, record=False).clean:
        raise ValueError("cannot approve an out-of-scope change")
    from .worker_preparation import validate_completed
    validate_completed(project, task, worktree)
    from .verification import run
    result = run(conn, project, worktree, task["gate_level"])
    if not result.passed or repo.head_commit(worktree) != head or not repo.is_clean(worktree):
        raise ValueError("the accepted commit failed its gate: " + result.summary())
    db.record_gate(conn, task_id, task["gate_level"], head, True, result.summary())
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (task_id, head, "PASS", f"user:{by}", str(db.redact(evidence))[:4000], db.utcnow()))
    db.set_status(conn, task_id, "INTEGRATION_READY", actor="human", cause="the user accepted the reviewed commit")
    db.update_task(conn, task_id, blocker=None, next_action="Accepted by the user; automatic integration follows.")
    db.log_event(conn, task_id, "review_approved", cause=evidence[:1000], detail={"head": head, "reviewer": f"user:{by}"})
    return head


def ensure_independent(conn: sqlite3.Connection, task: dict, process: dict) -> None:
    """A reviewer may share a model with the worker, never its session or process."""
    workers = conn.execute("SELECT id, session_token FROM processes WHERE task_id=? AND purpose='worker'",
                           (task["id"],)).fetchall()
    tokens = {str(r["session_token"]) for r in workers if r["session_token"]}
    if task.get("session_token"):
        tokens.add(str(task["session_token"]))
    if process["purpose"] == "worker" or process["id"] in {r["id"] for r in workers}:
        raise PermissionError("a worker session cannot review its own commit")
    if process.get("session_token") and str(process["session_token"]) in tokens:
        raise PermissionError("reviewer resumed the worker's session; a fresh independent session is required")


def invalidate(conn: sqlite3.Connection, task_id: int, head: str, reason: str) -> None:
    """Record that earlier verdicts no longer apply, keeping them as history."""
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (task_id, head or "", "INVALIDATED", "supervisor", reason, db.utcnow()))
    db.log_event(conn, task_id, "review_invalidated", cause=reason, detail={"head": head})


def require_approval(conn: sqlite3.Connection, task: dict, project=None) -> str:
    head = repo.head_commit(task["worktree"])
    row = conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if not row or row["verdict"] != "PASS" or row["head_sha"] != head:
        raise ValueError("the current worker commit has no independent PASS review")
    if project is not None:
        from .review_policy import GATE_REVIEWER, STAGING_REVIEWER, mode
        if row["reviewer"] in (STAGING_REVIEWER, GATE_REVIEWER):
            if row["reviewer"] == STAGING_REVIEWER and mode(project) != "human":
                raise ValueError("mechanical staging does not satisfy AI review policy")
            if row["reviewer"] == GATE_REVIEWER and mode(project) not in ("once", "off"):
                raise ValueError("a gate-only approval does not satisfy per-task AI review")
            from .verification import cached
            if not cached(conn, project, task["worktree"], task["gate_level"]):
                raise ValueError("mechanical staging check inputs changed; recheck current policy")
    gate = db.cached_gate(conn, task["id"], task["gate_level"], head)
    if not gate or not gate["passed"]:
        raise ValueError("the reviewed commit has no passing task gate")
    return head
