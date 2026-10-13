"""Review findings go straight back to the builder; the manager is not woken for them.

A CHANGES verdict on the current commit is routine iteration, not a decision: the
builder continues with the findings as its current instruction (a feature builder in
its own session). After the last attempt, or for REJECT, the task is a problem for the
user (problems.py); without a workflow the manager decides as before. A builder that
hands back the same commit stops the task (infra_stop) instead of looping.
"""
from __future__ import annotations

from pathlib import Path

from . import db, repo
from . import statemachine as sm

MAX_ATTEMPTS = 3


def due(conn, task) -> bool:
    if task["status"] != sm.FAILED or int(task["attempts"]) >= MAX_ATTEMPTS:
        return False
    row = conn.execute("SELECT head_sha,verdict FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task["id"],)).fetchone()
    work = task.get("worktree")
    return bool(row and row["verdict"] == "CHANGES" and work and Path(work).is_dir()
                and repo.head_commit(work) == row["head_sha"])


def requeue(conn, task) -> None:
    attempt = int(task["attempts"]) + 1
    db.update_task(conn, task["id"], attempts=attempt, blocker=None)
    db.set_status(conn, task["id"], sm.READY, cause="review asked for changes; the builder continues with the findings")
    db.log_event(conn, task["id"], "review_retry", effect="builder requeued with the review findings",
                 detail={"attempt": attempt, "max": MAX_ATTEMPTS})
