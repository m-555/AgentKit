"""Classified failure recovery (PLAN_V3 §13).

A generic "restart the agent" causes two specific harms: it repeats work that
already succeeded, and it retries tasks that are wrong rather than unlucky. Each
class below gets its own action.

The rule most systems get wrong is `PROVIDER_UNAVAILABLE`: a rate limit is not
the task's fault, so it must not consume an attempt. Burning attempts on outages
eventually pushes a perfectly healthy task into NEEDS_REPLAN.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, repo, worktrees
from . import statemachine as sm
from .config import ProjectConfig

MAX_ATTEMPTS = 3
MAX_REVIEW_REJECTS = 2


class Failure:
    CRASH = "agent_crash"
    CONTEXT_EXHAUSTED = "context_exhausted"
    STALLED = "stalled"
    GATE_FAILED = "gate_failed"
    REVIEW_REJECTED = "review_rejected"
    DIRTY_WORKTREE = "dirty_worktree"
    STALE_LEASE = "stale_lease"
    BUDGET = "budget_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MERGE_CONFLICT_MECHANICAL = "merge_conflict_mechanical"
    MERGE_CONFLICT_SEMANTIC = "merge_conflict_semantic"
    ENVIRONMENT = "environment_failure"
    LEASE_VIOLATION = "lease_violation"


@dataclass
class RecoveryAction:
    failure: str
    next_status: str | None
    consumes_attempt: bool
    relaunch: bool
    detail: str
    escalate: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure": self.failure, "next_status": self.next_status,
            "consumes_attempt": self.consumes_attempt, "relaunch": self.relaunch,
            "detail": self.detail, "escalate": self.escalate,
        }


def classify_exit(exit_code: int | None, stderr: str = "") -> str:
    """Map a worker exit into a failure class.

    Provider problems are separated from task problems deliberately: they look
    identical at the process level and must be treated oppositely.
    """
    text = (stderr or "").lower()
    for marker in ("rate limit", "429", "quota", "overloaded", "503", "unauthorized",
                   "authentication", "econnreset", "network"):
        if marker in text:
            return Failure.PROVIDER_UNAVAILABLE
    if "budget" in text or exit_code == 3:
        return Failure.BUDGET
    if exit_code in (None, 0):
        return Failure.CONTEXT_EXHAUSTED
    return Failure.CRASH


def decide(
    conn: sqlite3.Connection, task: dict[str, Any], failure: str, detail: str = ""
) -> RecoveryAction:
    """The §13 table, as code."""
    attempts = int(task.get("attempts") or 0)
    task_id = int(task["id"])

    if failure == Failure.PROVIDER_UNAVAILABLE:
        return RecoveryAction(
            failure, sm.READY, consumes_attempt=False, relaunch=True,
            detail="provider problem, not a task problem: requeued without consuming an attempt",
        )

    if failure == Failure.CONTEXT_EXHAUSTED:
        return RecoveryAction(
            failure, None, consumes_attempt=False, relaunch=True,
            detail="checkpoint written before compaction; continuing the same task",
        )

    if failure == Failure.CRASH:
        if attempts + 1 >= MAX_ATTEMPTS:
            return RecoveryAction(
                failure, sm.NEEDS_REPLAN, consumes_attempt=True, relaunch=False,
                detail=f"{attempts + 1} crashes: the task, not the run, is the problem",
                escalate="architect",
            )
        return RecoveryAction(
            failure, sm.READY, consumes_attempt=True, relaunch=True,
            detail="relaunching from the mechanical checkpoint at a new generation",
        )

    if failure == Failure.STALLED:
        return RecoveryAction(
            failure, sm.STALE, consumes_attempt=False, relaunch=False,
            detail="lease expired; worktree preserved. Adopt or discard explicitly — "
                   "AgentKit never kills a process it cannot prove is dead",
            escalate="human",
        )

    if failure == Failure.GATE_FAILED:
        if attempts + 1 >= MAX_ATTEMPTS:
            return RecoveryAction(
                failure, sm.NEEDS_REPLAN, consumes_attempt=True, relaunch=False,
                detail=f"gate failed {attempts + 1} times: stop retrying and re-scope",
                escalate="architect",
            )
        return RecoveryAction(
            failure, sm.RUNNING, consumes_attempt=True, relaunch=True,
            detail="returning the failure to the worker with the gate output attached",
        )

    if failure == Failure.REVIEW_REJECTED:
        rejects = _count_events(conn, task_id, "review_rejected")
        if rejects + 1 >= MAX_REVIEW_REJECTS:
            return RecoveryAction(
                failure, sm.NEEDS_REPLAN, consumes_attempt=True, relaunch=False,
                detail="wrong twice is a specification problem, not an execution one",
                escalate="human",
            )
        return RecoveryAction(
            failure, sm.RUNNING, consumes_attempt=True, relaunch=True,
            detail="returning the review findings to the worker",
        )

    if failure == Failure.DIRTY_WORKTREE:
        return RecoveryAction(
            failure, sm.STALE, consumes_attempt=False, relaunch=False,
            detail="worktree does not match the recorded HEAD; the dirt is evidence and is "
                   "snapshotted, never auto-cleaned",
            escalate="human",
        )

    if failure == Failure.STALE_LEASE:
        return RecoveryAction(
            failure, sm.STALE, consumes_attempt=False, relaunch=False,
            detail="lease expired; no auto-revoke while a process may still hold it",
            escalate="human",
        )

    if failure == Failure.BUDGET:
        return RecoveryAction(
            failure, sm.BLOCKED, consumes_attempt=False, relaunch=False,
            detail="budget cap reached; checkpoint written. Raise the cap or re-scope",
            escalate="human",
        )

    if failure == Failure.ENVIRONMENT:
        return RecoveryAction(
            failure, sm.READY, consumes_attempt=False, relaunch=True,
            detail="dependency/environment failure: re-provisioning before counting an attempt",
        )

    if failure == Failure.MERGE_CONFLICT_MECHANICAL:
        return RecoveryAction(
            failure, sm.INTEGRATION_READY, consumes_attempt=False, relaunch=False,
            detail="mechanical conflict; the integrator resolves imports and adjacent additions",
        )

    if failure == Failure.MERGE_CONFLICT_SEMANTIC:
        return RecoveryAction(
            failure, sm.REVIEW, consumes_attempt=False, relaunch=False,
            detail="semantic conflict between two tasks: a planning failure, not a merge one. "
                   "Overlap prediction should have serialised them",
            escalate="architect",
        )

    if failure == Failure.LEASE_VIOLATION:
        return RecoveryAction(
            failure, sm.FAILED, consumes_attempt=True, relaunch=False,
            detail="branch quarantined; out-of-lease changes are never auto-fixed",
            escalate="human",
        )

    return RecoveryAction(
        failure, sm.FAILED, consumes_attempt=True, relaunch=False,
        detail=detail or "unclassified failure", escalate="human",
    )


def _count_events(conn: sqlite3.Connection, task_id: int, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND kind = ?", (task_id, kind)
    ).fetchone()
    return int(row["n"]) if row else 0


def apply(
    conn: sqlite3.Connection, task_id: int, action: RecoveryAction, *, actor: str = "scheduler"
) -> None:
    """Record the decision and move the task, once, legally."""
    task = db.get_task(conn, task_id)
    if task is None:
        return
    if action.consumes_attempt:
        db.update_task(conn, task_id, attempts=int(task.get("attempts") or 0) + 1)
    if action.next_status and sm.can(str(task["status"]), action.next_status, actor):
        db.set_status(conn, task_id, action.next_status, actor=actor,
                      cause=f"{action.failure}: {action.detail}")
    if action.next_status in (sm.STALE, sm.FAILED, sm.BLOCKED, sm.NEEDS_REPLAN):
        db.release_leases(conn, task_id, reason=action.failure)
    db.update_task(conn, task_id, blocker=action.detail[:300] if action.escalate else None)
    db.log_event(
        conn, task_id, "recovery_decision",
        cause=action.failure, effect=action.detail,
        detail=action.to_dict(),
    )


def inspect_worktree(
    conn: sqlite3.Connection, project: ProjectConfig, task: dict[str, Any]
) -> str | None:
    """Detect drift between the recorded HEAD and reality (failure class 6)."""
    worktree = task.get("worktree")
    if not worktree or not Path(str(worktree)).is_dir():
        return None
    checkpoint = db.latest_checkpoint(conn, int(task["id"]), kind="mechanical")
    if not checkpoint:
        return None
    recorded = str((checkpoint.get("payload") or {}).get("head_sha") or "")
    live = repo.head_commit(str(worktree))
    if recorded and live and recorded != live:
        return f"recorded HEAD {recorded} but worktree is at {live}"
    if not repo.is_clean(str(worktree)):
        return f"{len(repo.changed_files(str(worktree)))} uncommitted file(s) after a crash"
    return None


def adopt(conn: sqlite3.Connection, task_id: int) -> str:
    """Re-attach a STALE task to a fresh worker. A human decision (§16)."""
    task = db.get_task(conn, task_id)
    if task is None:
        return f"task {task_id} does not exist"
    if str(task["status"]) != sm.STALE:
        return f"task {task_id} is {task['status']}, not STALE"
    generation = db.bump_generation(conn, task_id)
    db.set_status(conn, task_id, sm.READY, actor="human",
                  cause="adopted by the operator; worktree preserved")
    db.update_task(conn, task_id, blocker=None)
    db.log_event(conn, task_id, "task_adopted",
                 effect=f"generation {generation}; previous worker's claims invalidated")
    return f"task {task_id} adopted at generation {generation}"


def discard(conn: sqlite3.Connection, root: str | Path, task_id: int) -> str:
    """Throw away a STALE task's worktree and start it over."""
    task = db.get_task(conn, task_id)
    if task is None:
        return f"task {task_id} does not exist"
    worktrees.remove(root, task, force=True)
    db.bump_generation(conn, task_id)
    db.release_leases(conn, task_id, reason="discarded")
    # An operator task is a human's abandoned claim, not queued work. Resetting it
    # to READY put it in front of the scheduler, where it sat as a phantom candidate
    # that could never complete — and, having no requirements of its own, one the
    # capability check would happily assign a real agent to.
    from .leases import OPERATOR_KIND

    is_operator = str(task.get("kind") or "") == OPERATOR_KIND
    target = sm.CANCELLED if is_operator else sm.READY
    if sm.can(str(task["status"]), target, "human"):
        db.set_status(conn, task_id, target, actor="human", cause="worktree discarded")
    db.update_task(conn, task_id, worktree=None, base_sha=None, blocker=None)
    db.log_event(conn, task_id, "task_discarded",
                 effect=f"worktree removed; task set to {target}")
    return f"task {task_id} discarded and set to {target}"
