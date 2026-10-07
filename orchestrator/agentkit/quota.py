"""Pausing and waking the tasks a provider cooldown affects.

The split that matters: `providers.py` owns *account* availability, this module
owns what happens to the *tasks* that were using it. Keeping them apart is what
lets Codex keep working while Claude waits.

Pausing uses the existing `BLOCKED` state with structured metadata rather than a
new state, and a quota pause never touches `attempts` â€” being throttled is not a
failed implementation, and counting it as one is how a healthy task eventually
gets marked NEEDS_REPLAN.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import db, globs, providers
from . import statemachine as sm

REASON = "provider_usage_limit"
HEALTH_REASON = "provider_health_unavailable"

#: Lease TTL while paused. A subscription window is hours, and a paused task's
#: lease must not quietly expire and hand its hotspot to somebody else.
PAUSED_LEASE_TTL_SECONDS = 8 * 3600


@dataclass
class PauseResult:
    task_id: int
    provider: str
    retry_at: str | None
    lease_kept: bool
    lease_reason: str

    def summary(self) -> str:
        held = "lease reserved" if self.lease_kept else "lease released"
        return (
            f"task {self.task_id} paused on {self.provider} until "
            f"{self.retry_at or 'unknown'} ({held}: {self.lease_reason})"
        )


def blocked_meta(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("blocked_meta")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_quota_paused(task: dict[str, Any]) -> bool:
    return (
        str(task.get("status")) == sm.BLOCKED
        and blocked_meta(task).get("reason") in (REASON, HEALTH_REASON)
    )


def pause(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    provider: str,
    account: str = "default",
    retry_at: str | None,
    raw_message: str = "",
    health: bool = False,
) -> PauseResult:
    """Park a task until its provider recovers, preserving everything it needs.

    Worktree, branch, base_sha and checkpoints are all left alone; only the
    worker slot is given up, because holding a process for five hours helps
    nobody.
    """
    if providers.unmetered(provider) and not health:
        raise ValueError("unmetered local providers require health recovery, not a quota pause")
    task = db.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} does not exist")

    keep, why = should_reserve_lease(conn, task)
    if keep:
        _extend_lease(conn, task_id, retry_at)
    else:
        db.release_leases(conn, task_id, reason=f"{REASON}: {why}")

    meta = {
        "reason": HEALTH_REASON if health else REASON,
        "provider": provider,
        "account": account,
        "retry_at": retry_at,
        "generation": int(task.get("generation") or 0),
        "paused_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lease_reserved": keep,
        "raw_message": raw_message[:500],
    }
    db.update_task(conn, task_id, blocked_meta=json.dumps(meta),
                   blocker=f"{provider} health unavailable; next check {retry_at or 'when healthy'}" if health else f"{provider} usage limit; resumes {retry_at or 'when available'}")

    current = str(task["status"])
    if sm.can(current, sm.BLOCKED, "scheduler"):
        db.set_status(conn, task_id, sm.BLOCKED, actor="scheduler",
                      cause=f"{provider} usage limit reached")
    db.log_event(
        conn, task_id, "health_paused" if health else "quota_paused",
        cause=f"{provider} health unavailable" if health else f"{provider} usage limit",
        effect=f"BLOCKED until {retry_at or 'provider recovery'}; "
               f"{'lease reserved' if keep else 'lease released'}",
        detail=meta,
    )
    from .recovery_runtime import park_worker
    park_worker(conn, db.get_task(conn, task_id), meta)
    return PauseResult(task_id, provider, retry_at, keep, why)


def should_reserve_lease(
    conn: sqlite3.Connection, task: dict[str, Any]
) -> tuple[bool, str]:
    """Keep the lease only when releasing it would make resuming unsafe.

    Uses the existing conflict logic rather than a new rule: if any other
    non-terminal task could claim these paths, releasing would let it edit files
    this task has half-finished, and the paused work could not be resumed
    safely. If nobody else wants them, the lease is released so the state stays
    tidy â€” reacquiring on wake is cheap and conflict-checked anyway.
    """
    task_id = int(task["id"])
    held = [
        str(lease["path_glob"]) for lease in db.active_leases(conn)
        if int(lease["task_id"]) == task_id
    ]
    if not held:
        return False, "task holds no leases"

    for other in db.list_tasks(conn):
        if int(other["id"]) == task_id or str(other["status"]) in sm.TERMINAL:
            continue
        wanted = [
            *(other.get("expected_write") or []),
            *(other.get("owned_paths") or []),
        ]
        for pattern in wanted:
            for mine in held:
                if globs.overlaps(mine, str(pattern)):
                    return True, (
                        f"task {other['id']} wants {pattern}, which overlaps "
                        f"{mine}; releasing would make this task unsafe to resume"
                    )
    return False, "no other task wants these paths"


def _extend_lease(conn: sqlite3.Connection, task_id: int, retry_at: str | None = None) -> None:
    """Stretch the lease over the cooldown so it cannot expire while paused."""
    now = datetime.now(UTC)
    reset = db.parse_ts(retry_at)
    ttl = max(PAUSED_LEASE_TTL_SECONDS, int((reset - now).total_seconds()) + 3600 if reset else 0)
    expires = now.timestamp() + ttl
    conn.execute(
        "UPDATE leases SET ttl_seconds = ?, heartbeat_at = ?, expires_at = ? "
        "WHERE task_id = ? AND released_at IS NULL",
        (ttl, now.isoformat(timespec="seconds"),
         datetime.fromtimestamp(expires, UTC).isoformat(timespec="seconds"), task_id),
    )


def wake_ready(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Return quota-paused tasks whose provider is available again, and free them.

    Automatic: this is what makes "leave the PC and come back to finished work"
    true rather than aspirational.
    """
    moment = now or datetime.now(UTC)
    woken: list[int] = []

    for task in db.list_tasks(conn, (sm.BLOCKED,)):
        if not is_quota_paused(task):
            continue
        meta = blocked_meta(task)
        provider = str(meta.get("provider") or "")
        account = str(meta.get("account") or "default")
        if not providers.is_available(conn, provider, account, moment):
            continue

        from .recovery_runtime import worker_ready
        if not worker_ready(conn, task, meta):
            continue
        task_id = int(task["id"])
        if not sm.can(str(task["status"]), sm.READY, "scheduler"):
            continue
        db.update_task(conn, task_id, blocked_meta=None, blocker=None)
        db.set_status(conn, task_id, sm.READY, actor="scheduler",
                      cause=f"{provider} available again")
        db.log_event(
            conn, task_id, "quota_resumed",
            cause=f"{provider} recovered at {meta.get('retry_at')}",
            effect="BLOCKED -> READY; worker will resume from its checkpoint",
            detail={"provider": provider, "paused_at": meta.get("paused_at")},
        )
        woken.append(task_id)
    return woken


def paused_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [t for t in db.list_tasks(conn, (sm.BLOCKED,)) if is_quota_paused(t)]


def handle_worker_failure(
    conn: sqlite3.Connection,
    task_id: int,
    adapter_name: str,
    text: str,
    exit_code: int | None,
) -> str | None:
    """Classify a worker's exit and apply the provider-level response.

    Returns the error class when it was a provider problem and has been handled
    here, or None when the caller should fall through to ordinary task recovery.
    """
    from . import adapters, errors

    adapter = adapters.get(adapter_name)
    result = (
        adapter.classify_error(text, exit_code) if adapter
        else errors.with_retry_at(errors.classify(text, exit_code))
    )
    if not result.is_provider_problem:
        return None

    from .policy import record_trigger
    task = db.get_task(conn, task_id) or {}
    record_trigger(conn, task_id, result.kind, adapter_name, str(task.get("model") or ""))
    account = adapter.account_id() if adapter else "default"
    if result.cools_provider:
        state = providers.begin_cooldown(
            conn, adapter_name, reason=result.reason, raw_message=result.raw,
            retry_at=result.retry_at, account=account,
        )
        pause(conn, task_id, provider=adapter_name, account=account,
              retry_at=state.retry_at, raw_message=result.raw, health=providers.unmetered(adapter_name))
    else:
        # AUTH_ERROR: a human must fix it, so do not schedule a retry storm.
        providers.observe(conn, adapter_name, {"available": False, "auth_error": True, "reason": result.reason}, account=account)
        pause(conn, task_id, provider=adapter_name, account=account,
              retry_at=None, raw_message=result.raw, health=providers.unmetered(adapter_name))
    return result.kind
