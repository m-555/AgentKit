"""The task lifecycle â€” one authoritative vocabulary.

Invariant 9: every state transition is legal, logged and explainable. Agents,
hooks and the scheduler all route through `validate`, which is why three
components cannot end up inventing four vocabularies (PLAN_V3 Â§5).
"""

from __future__ import annotations

from dataclasses import dataclass

PLANNED = "PLANNED"
READY = "READY"
LEASED = "LEASED"
RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
REVIEW = "REVIEW"
INTEGRATION_READY = "INTEGRATION_READY"
INTEGRATING = "INTEGRATING"
DONE = "DONE"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
STALE = "STALE"
NEEDS_REPLAN = "NEEDS_REPLAN"
CANCELLED = "CANCELLED"

STATES = (
    PLANNED, READY, LEASED, RUNNING, VERIFYING, REVIEW, INTEGRATION_READY,
    INTEGRATING, DONE, BLOCKED, FAILED, STALE, NEEDS_REPLAN, CANCELLED,
)

TERMINAL = (DONE, CANCELLED)

#: States in which a task's claims block other tasks (PLAN_V3 Â§2.4).
ACTIVE = (READY, LEASED, RUNNING, VERIFYING, REVIEW, INTEGRATION_READY, INTEGRATING)

#: States whose path claims block other tasks.
#:
#: BLOCKED is included but ACTIVE is not: a task paused on a provider cooldown
#: still owns its worktree and its half-finished work, so releasing its claim
#: would let another task edit the same files and make the paused work unsafe to
#: resume. Scheduling still uses ACTIVE â€” a BLOCKED task is not runnable.
HOLDS_CLAIMS = (*ACTIVE, BLOCKED)

#: States in which a worker may be running and holding a worktree.
OCCUPIES_WORKTREE = (LEASED, RUNNING, VERIFYING, REVIEW, INTEGRATION_READY, INTEGRATING, STALE)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    PLANNED: (READY, CANCELLED, NEEDS_REPLAN),
    READY: (LEASED, BLOCKED, CANCELLED, NEEDS_REPLAN, PLANNED),
    # READY is reachable from every active state: a worker that stops without
    # finishing is requeued, not failed. A provider outage is the common case and
    # routing it through FAILED would both misreport it and burn a retry.
    # NEEDS_REPLAN is reachable for the same reason Â§4.3 needs it â€” a spec edited
    # mid-flight must be able to stop a running task immediately.
    LEASED: (RUNNING, READY, STALE, FAILED, NEEDS_REPLAN, CANCELLED),
    RUNNING: (VERIFYING, BLOCKED, READY, STALE, FAILED, NEEDS_REPLAN, CANCELLED),
    VERIFYING: (REVIEW, FAILED, RUNNING, READY, NEEDS_REPLAN, BLOCKED),
    REVIEW: (INTEGRATION_READY, FAILED, NEEDS_REPLAN, RUNNING),
    INTEGRATION_READY: (INTEGRATING, FAILED, NEEDS_REPLAN),
    INTEGRATING: (DONE, FAILED),
    BLOCKED: (READY, RUNNING, VERIFYING, NEEDS_REPLAN, CANCELLED),
    FAILED: (READY, VERIFYING, INTEGRATION_READY, NEEDS_REPLAN, CANCELLED),
    STALE: (RUNNING, READY, VERIFYING, NEEDS_REPLAN, CANCELLED),
    NEEDS_REPLAN: (PLANNED, READY, CANCELLED),
    DONE: (),
    CANCELLED: (),
}

#: Which actor may request which transition. Hooks are deliberately the weakest.
ACTOR_PERMISSIONS: dict[str, frozenset[str]] = {
    "scheduler": frozenset(STATES),
    "integrator": frozenset({INTEGRATION_READY, INTEGRATING, DONE, FAILED, REVIEW}),
    "reviewer": frozenset({INTEGRATION_READY, FAILED}),
    # An agent may report progress and problems, but may never requeue itself,
    # replan itself, or declare its own work integration-ready.
    "agent": frozenset({RUNNING, VERIFYING, REVIEW, BLOCKED, FAILED}),
    "hook": frozenset({VERIFYING, STALE}),
    "human": frozenset(STATES),
}


@dataclass
class TransitionError(Exception):
    current: str
    requested: str
    reason: str

    def __str__(self) -> str:
        return (
            f"illegal transition {self.current} -> {self.requested}: {self.reason}. "
            f"Legal from {self.current}: {', '.join(TRANSITIONS.get(self.current, ())) or 'none'}"
        )


def validate(current: str, requested: str, actor: str = "scheduler") -> None:
    """Raise TransitionError unless this actor may make this move."""
    if requested not in STATES:
        raise TransitionError(current, requested, f"{requested!r} is not a task state")
    if current not in STATES:
        raise TransitionError(current, requested, f"current state {current!r} is unknown")
    if current == requested:
        return
    if current in (FAILED, BLOCKED, STALE) and requested == VERIFYING and actor not in ("human", "scheduler"):
        raise TransitionError(current, requested, "only the host may recover preserved failed work")
    allowed = TRANSITIONS.get(current, ())
    if requested not in allowed:
        raise TransitionError(current, requested, "not reachable from the current state")
    permitted = ACTOR_PERMISSIONS.get(actor)
    if permitted is None:
        raise TransitionError(current, requested, f"unknown actor {actor!r}")
    if requested not in permitted:
        raise TransitionError(
            current, requested, f"actor {actor!r} may not set {requested}"
        )


def can(current: str, requested: str, actor: str = "scheduler") -> bool:
    try:
        validate(current, requested, actor)
        return True
    except TransitionError:
        return False


def is_active(status: str) -> bool:
    return status in ACTIVE


def is_terminal(status: str) -> bool:
    return status in TERMINAL
