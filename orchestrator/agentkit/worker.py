"""One authoritative check for "is this caller still the current worker?" (§6).

Generation protects against zombies: a worker launched at generation 2 whose task
has since been recovered to generation 3 must not be able to mutate anything. The
danger is not that the check is hard — it is that it gets written ten times and
one of them is forgotten. Every worker-originated mutation routes through
`require_current` here, so there is exactly one implementation to audit.

Read-only calls deliberately do not require a current generation: a stale worker
asking "what is my brief?" should get an answer explaining that it is stale,
rather than an opaque error.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db
from .context import active_generation, active_task_id


class StaleGeneration(RuntimeError):
    """A worker from a superseded generation attempted a mutation."""

    def __init__(self, task_id: int, worker_generation: int | None, current: int):
        self.task_id = task_id
        self.worker_generation = worker_generation
        self.current = current
        super().__init__(
            f"task {task_id} is at generation {current}; this worker was launched at "
            f"generation {worker_generation}. Its lease and worktree have been "
            "reassigned, so state changes from it are refused. Stop work and exit."
        )


@dataclass
class WorkerContext:
    task_id: int
    worker_generation: int | None
    current_generation: int
    task: dict[str, Any]

    @property
    def is_current(self) -> bool:
        """An unset worker generation means a human or a tool, not a zombie."""
        if self.worker_generation is None:
            return True
        return int(self.worker_generation) == int(self.current_generation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "worker_generation": self.worker_generation,
            "current_generation": self.current_generation,
            "is_current": self.is_current,
        }


def resolve_task_id(explicit: int | None = None, cwd: str | None = None) -> int | None:
    return explicit if explicit is not None else active_task_id(cwd)


def context(
    conn: sqlite3.Connection, task_id: int, *, worker_generation: int | None = None
) -> WorkerContext | None:
    task = db.get_task(conn, task_id)
    if task is None:
        return None
    generation = (
        worker_generation if worker_generation is not None else active_generation()
    )
    return WorkerContext(
        task_id=task_id,
        worker_generation=generation,
        current_generation=int(task.get("generation") or 0),
        task=task,
    )


def require_current(
    conn: sqlite3.Connection, task_id: int, *, worker_generation: int | None = None
) -> WorkerContext:
    """Gate every worker-originated mutation. Raises `StaleGeneration` if superseded.

    Records the attempt before raising: a zombie still trying to write is a fact
    worth seeing in the event log, not a silent no-op.
    """
    ctx = context(conn, task_id, worker_generation=worker_generation)
    active = active_task_id()
    if active is not None and active != task_id:
        raise PermissionError("a worker may mutate only its assigned task")
    if ctx is None:
        raise ValueError(f"task {task_id} does not exist")
    if not ctx.is_current:
        db.log_event(
            conn, task_id, "stale_generation",
            cause=(
                f"worker generation {ctx.worker_generation} < current "
                f"{ctx.current_generation}"
            ),
            effect="mutation refused; no state changed",
            detail=ctx.to_dict(),
        )
        raise StaleGeneration(task_id, ctx.worker_generation, ctx.current_generation)
    return ctx


def guard(conn: sqlite3.Connection, task_id: int | None) -> WorkerContext | None:
    """Non-raising form for hooks, which must never crash a session.

    Returns None when there is nothing to check; the caller treats a returned
    context with `is_current == False` as "do not mutate".
    """
    if task_id is None:
        return None
    try:
        return require_current(conn, task_id)
    except (StaleGeneration, ValueError):
        return context(conn, task_id)
