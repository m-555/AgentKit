"""The human escape hatch, built on the lease model rather than beside it (§3).

Denying writes to an AgentKit-managed repository whenever a session has no task
would make the tool unusable — you sometimes need to edit your own code. The
answer is not an implicit bypass but an explicit, conflict-checked, logged claim:
the operator takes a lease like any worker, and every guarantee continues to hold
while they hold it.

Consequences that follow from reusing the lease model rather than special-casing
humans:

* an operator cannot take a path a running worker holds — they must resolve that
  lease through the normal human-controlled revocation path first;
* a worker cannot take a path the operator holds;
* the claim appears in `agentkit status` and in the event log;
* the lease expires on its own, so a forgotten claim does not block the queue
  forever.
"""

from __future__ import annotations

import getpass
import socket
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from . import statemachine as sm
from .leases import OPERATOR_KIND

OPERATOR_SPEC_ID = "operator"
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_HOURS = 8


@dataclass
class OperatorResult:
    ok: bool
    message: str
    task_id: int | None = None
    granted: list[str] | None = None
    conflicts: list[dict[str, Any]] | None = None

    def summary(self) -> str:
        lines = [self.message]
        for conflict in self.conflicts or []:
            lines.append(
                f"  task {conflict['task_id']} ({conflict['task_title']}) holds "
                f"`{conflict['held']}`"
            )
        return "\n".join(lines)


def _who() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "operator"


def ensure_task(conn: sqlite3.Connection) -> int:
    """One durable operator task per repository, reused across claims."""
    existing = db.get_task_by_spec(conn, OPERATOR_SPEC_ID)
    if existing is not None:
        task_id = int(existing["id"])
        if str(existing["status"]) not in sm.ACTIVE:
            db.update_task(conn, task_id, status=sm.RUNNING)
        return task_id
    return db.create_task(
        conn,
        spec_id=OPERATOR_SPEC_ID,
        title=f"operator ({_who()})",
        description="Manual edits by a human. Holds leases like any other task.",
        kind=OPERATOR_KIND,
        role="operator",
        status=sm.RUNNING,
    )


def acquire(
    conn: sqlite3.Connection,
    paths: list[str],
    *,
    reason: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> OperatorResult:
    cleaned = [p.strip() for p in paths if p and p.strip()]
    if not cleaned:
        return OperatorResult(False, "No paths given. Pass the paths you intend to edit.")

    task_id = ensure_task(conn)
    granted, conflicts = db.try_acquire_leases(
        conn, task_id, cleaned, mode="exclusive-write",
        ttl_seconds=ttl_seconds, max_hours=DEFAULT_MAX_HOURS,
    )
    if conflicts:
        db.log_event(
            conn, task_id, "operator_lease_refused",
            cause=f"{_who()} requested {', '.join(cleaned)}",
            effect="refused; a running task holds an overlapping path",
            detail={"conflicts": conflicts, "reason": reason},
        )
        return OperatorResult(
            False,
            "Refused: a running task holds an overlapping path. Let it finish, or "
            "revoke its lease explicitly with `agentkit lease revoke`.",
            task_id=task_id, conflicts=conflicts,
        )

    existing = db.get_task(conn, task_id) or {}
    merged = sorted({*(existing.get("owned_paths") or []), *cleaned})
    db.update_task(conn, task_id, owned_paths=merged)
    db.heartbeat(conn, task_id)
    db.log_event(
        conn, task_id, "operator_lease_acquired",
        cause=f"{_who()}: {reason or 'manual edits'}",
        effect=f"holds {', '.join(cleaned)} for up to {ttl_seconds // 60} min",
        detail={"paths": cleaned, "ttl_seconds": ttl_seconds},
    )
    return OperatorResult(
        True,
        f"Operator lease granted on {', '.join(cleaned)}.\n"
        f"Workers cannot take these paths while you hold them. "
        f"Release with `agentkit operator release` when you are done.",
        task_id=task_id, granted=granted,
    )


def release(conn: sqlite3.Connection) -> OperatorResult:
    task = db.get_task_by_spec(conn, OPERATOR_SPEC_ID)
    if task is None:
        return OperatorResult(True, "No operator lease is held.")
    task_id = int(task["id"])
    count = db.release_leases(conn, task_id, reason="operator released")
    db.update_task(conn, task_id, owned_paths=[])
    # Stand the singleton back down. Leaving it RUNNING with no worker process and
    # no leases is what made every claim/release cycle leave litter behind: the next
    # `reconcile` finds a RUNNING task whose pid does not exist, marks it STALE, and
    # asks a human to adopt or discard a task that merely finished normally.
    # CANCELLED is terminal, holds no claims and is never scheduled, and
    # `ensure_task` revives it on the next acquire — so the singleton is reusable
    # without being permanently "in flight".
    current = str(task["status"])
    if sm.can(current, sm.CANCELLED, "human"):
        db.set_status(conn, task_id, sm.CANCELLED, actor="human",
                      cause="operator lease released")
    db.log_event(
        conn, task_id, "operator_lease_released",
        cause=_who(), effect=f"released {count} lease(s); claim stood down",
    )
    promoted = db.refresh_ready(conn)
    message = f"Released {count} operator lease(s)."
    if promoted:
        message += f" Now READY: {', '.join(str(p) for p in promoted)}."
    return OperatorResult(True, message, task_id=task_id)


def status(conn: sqlite3.Connection) -> OperatorResult:
    task = db.get_task_by_spec(conn, OPERATOR_SPEC_ID)
    if task is None:
        return OperatorResult(True, "No operator lease is held.")
    task_id = int(task["id"])
    held = [
        str(lease["path_glob"]) for lease in db.active_leases(conn)
        if int(lease["task_id"]) == task_id
    ]
    if not held:
        return OperatorResult(True, "No operator lease is held.", task_id=task_id)
    return OperatorResult(
        True, "Operator lease held on:\n" + "\n".join(f"  {p}" for p in held),
        task_id=task_id, granted=held,
    )


def heartbeat(root: str | Path) -> None:
    """Keep a long editing session's claim alive."""
    conn = db.connect(root)
    try:
        task = db.get_task_by_spec(conn, OPERATOR_SPEC_ID)
        if task is not None:
            db.heartbeat(conn, int(task["id"]))
    finally:
        conn.close()
