"""Provider/account availability — cooling one account, not the orchestrator.

A subscription usage limit is an *account* fact, not a task fact. When Claude's
five-hour allowance runs out, every Claude worker sharing that account is
unavailable until it resets, while Codex workers carry on untouched. Modelling it
as a sleep inside the scheduler would be wrong twice over: it would stall
providers that are perfectly healthy, and it would forget the cooldown the moment
AgentKit restarted.

So availability lives in the database, keyed by provider account, and the
scheduler consults it the same way it consults leases.

Honest note on quota: a cooldown does not create allowance. Running more workers
in parallel consumes the same account budget faster. What this buys is that the
machine keeps doing whatever work *is* possible, and picks the paused work back
up without you being there.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db

AVAILABLE = "AVAILABLE"
COOLDOWN = "COOLDOWN"
DEGRADED = "DEGRADED"
AUTH_REQUIRED = "AUTH_REQUIRED"

#: Used when the provider tells us nothing about when it will be back.
UNKNOWN_RESET_BACKOFF = (900, 1800, 3600)        # 15 min, 30 min, 1 h
MAX_BACKOFF_SECONDS = 3600

#: A usage limit that reports no reset time still should not be retried for a
#: while — a subscription window is measured in hours, not seconds.
DEFAULT_USAGE_LIMIT_SECONDS = 1800


@dataclass
class ProviderState:
    provider: str
    account: str
    status: str
    reason: str = ""
    detected_at: str = ""
    retry_at: str | None = None
    raw_message: str = ""
    consecutive: int = 0

    @property
    def key(self) -> str:
        return account_key(self.provider, self.account)

    def available_at(self, now: datetime | None = None) -> bool:
        return self.status == AVAILABLE

    def due(self, now: datetime | None = None) -> bool:
        if self.status == AUTH_REQUIRED:
            return False
        retry = db.parse_ts(self.retry_at)
        return retry is None or retry <= (now or datetime.now(UTC))

    def seconds_remaining(self, now: datetime | None = None) -> int:
        retry = db.parse_ts(self.retry_at)
        if retry is None:
            return 0
        delta = (retry - (now or datetime.now(UTC))).total_seconds()
        return max(0, int(delta))

    def describe(self) -> str:
        if self.status == AVAILABLE:
            return f"{self.provider} AVAILABLE"
        when = db.parse_ts(self.retry_at)
        stamp = when.astimezone().strftime("%H:%M") if when else "unknown"
        return f"{self.provider} {self.status} {'retry' if self.status == DEGRADED else 'resets'} {stamp}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "account": self.account, "status": self.status,
            "reason": self.reason, "detected_at": self.detected_at,
            "retry_at": self.retry_at, "raw_message": self.raw_message,
            "consecutive": self.consecutive,
        }


def account_key(provider: str, account: str = "default") -> str:
    """Workers sharing an account share its allowance, and so share its cooldown."""
    return f"{provider}:{account or 'default'}"


def unmetered(provider: str) -> bool:
    """Explicit adapter policy, never inferred from a model name or token usage."""
    from . import adapters
    return getattr(adapters.get(provider), "allowance_policy", None) == "unmetered"


# ------------------------------------------------------------- retry_at parsing

_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)")
_CLOCK_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.IGNORECASE)
_CLOCK_24H = re.compile(r"\b(?:at|until|reset[s]?(?:\s+at)?)\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE)
_IN_DURATION = re.compile(
    r"\bin\s+(?:about\s+)?(\d+)\s*(second|minute|hour|min|sec|hr)s?\b", re.IGNORECASE
)


def parse_retry_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """Pull an absolute reset time out of a provider message.

    Handles `3pm`, `3:30 pm`, `15:00`, `in 42 minutes`, and explicit ISO
    timestamps. A clock time that has already passed today is taken to mean
    tomorrow — the usual case when a limit resets across midnight.

    Returns None when nothing can be parsed, which the caller must treat as
    "back off", never as "retry immediately".
    """
    if not text:
        return None
    moment = (now or datetime.now(UTC)).astimezone()

    match = _ISO.search(text)
    if match:
        raw = match.group(1).replace(" ", "T").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=moment.tzinfo)

    match = _IN_DURATION.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        seconds = amount * {
            "second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
        }[unit]
        return moment + timedelta(seconds=seconds)

    # Never interpret a weekly reset's clock as tomorrow. Parse the weekday first.
    weekday = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b", text, re.I)
    match = _CLOCK_12H.search(text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "p":
            hour += 12
        if not 0 <= minute <= 59 or not 1 <= int(match.group(1)) <= 12:
            return None
        if weekday:
            day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(weekday.group(1).lower())
            candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=(day - moment.weekday()) % 7)
            return candidate + timedelta(days=7) if candidate <= moment else candidate
        if "weekly" in text.lower() or "seven_day" in text.lower():
            return None
        return _next_occurrence(moment, hour, minute)

    match = _CLOCK_24H.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            if weekday:
                day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(weekday.group(1).lower())
                candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=(day - moment.weekday()) % 7)
                return candidate + timedelta(days=7) if candidate <= moment else candidate
            if "weekly" in text.lower() or "seven_day" in text.lower():
                return None
            return _next_occurrence(moment, hour, minute)
    return None


def _next_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    """The next time the clock reads hour:minute — tomorrow if already past."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ------------------------------------------------------------------ persistence


def get_state(conn: sqlite3.Connection, provider: str, account: str = "default") -> ProviderState:
    row = conn.execute(
        "SELECT * FROM provider_state WHERE account_key = ?",
        (account_key(provider, account),),
    ).fetchone()
    if row is None:
        return ProviderState(provider=provider, account=account, status=AVAILABLE)
    data = dict(row)
    return ProviderState(
        provider=str(data.get("provider") or provider),
        account=str(data.get("account") or account),
        status=str(data.get("status") or AVAILABLE),
        reason=str(data.get("reason") or ""),
        detected_at=str(data.get("detected_at") or ""),
        retry_at=data.get("retry_at"),
        raw_message=str(data.get("raw_message") or ""),
        consecutive=int(data.get("consecutive") or 0),
    )


def list_states(conn: sqlite3.Connection) -> list[ProviderState]:
    rows = conn.execute("SELECT * FROM provider_state ORDER BY account_key").fetchall()
    return [get_state(conn, str(r["provider"]), str(r["account"])) for r in rows]


def is_available(
    conn: sqlite3.Connection, provider: str, account: str = "default",
    now: datetime | None = None,
) -> bool:
    return get_state(conn, provider, account).available_at(now)


def begin_cooldown(
    conn: sqlite3.Connection,
    provider: str,
    *,
    reason: str,
    raw_message: str = "",
    retry_at: datetime | None = None,
    account: str = "default",
    default_seconds: int = DEFAULT_USAGE_LIMIT_SECONDS,
) -> ProviderState:
    """Mark an account unavailable. Idempotent: re-reporting extends, never stacks.

    With no `retry_at` from the provider, consecutive reports back off
    (15m → 30m → 1h) rather than polling a wall we already know is there.
    """
    previous = get_state(conn, provider, account)
    now = datetime.now(UTC)
    if unmetered(provider):
        # Local health/capacity uses seconds of backoff, not subscription clocks.
        consecutive = previous.consecutive + 1 if previous.status != AVAILABLE else 1
        state = ProviderState(provider=provider, account=account, status=DEGRADED,
            reason=reason, detected_at=now.isoformat(timespec="seconds"),
            retry_at=(now + timedelta(seconds=min(5 * 2 ** min(consecutive - 1, 4), 60))).isoformat(timespec="seconds"),
            raw_message=raw_message[:2000], consecutive=consecutive)
        conn.execute("DELETE FROM quota_windows WHERE account_key=?", (state.key,))
        _write(conn, state)
        db.log_event(conn, None, "provider_health_backoff", cause=reason, detail=state.to_dict())
        return state
    previous_retry = db.parse_ts(previous.retry_at)
    if retry_at is not None and previous.status != AVAILABLE and previous_retry and previous_retry > retry_at:
        retry_at = previous_retry
    consecutive = previous.consecutive + 1 if previous.status != AVAILABLE else 1

    if retry_at is None:
        if previous.status != AVAILABLE and previous.retry_at:
            existing = db.parse_ts(previous.retry_at)
            if existing and existing > now:
                retry_at = existing          # keep the provider's own answer
        if retry_at is None:
            index = min(consecutive - 1, len(UNKNOWN_RESET_BACKOFF) - 1)
            seconds = (
                default_seconds if consecutive == 1 else UNKNOWN_RESET_BACKOFF[index]
            )
            retry_at = now + timedelta(seconds=min(seconds, MAX_BACKOFF_SECONDS))

    state = ProviderState(
        provider=provider, account=account, status=COOLDOWN, reason=reason,
        detected_at=now.isoformat(timespec="seconds"),
        retry_at=retry_at.isoformat(timespec="seconds"),
        raw_message=raw_message[:2000], consecutive=consecutive,
    )
    _write(conn, state)
    db.log_event(
        conn, None, "provider_cooldown",
        cause=f"{provider} ({account}): {reason}",
        effect=f"unavailable until {state.retry_at}",
        detail=state.to_dict(),
    )
    return state


def clear_cooldown(
    conn: sqlite3.Connection, provider: str, account: str = "default", *, reason: str = "recovered"
) -> ProviderState:
    previous = get_state(conn, provider, account)
    state = ProviderState(
        provider=provider, account=account, status=AVAILABLE,
        detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        consecutive=0,
    )
    _write(conn, state)
    if previous.status != AVAILABLE:
        db.log_event(
            conn, None, "provider_available",
            cause=reason, effect=f"{provider} ({account}) is usable again",
            detail={"provider": provider, "account": account},
        )
    return state


def refresh(conn: sqlite3.Connection, now: datetime | None = None, *, checker=None) -> list[ProviderState]:
    """A timer permits a check; only fresh provider evidence restores availability."""
    from . import adapters
    moment = now or datetime.now(UTC)
    recovered = []
    for state in list_states(conn):
        if state.status != AVAILABLE and state.due(moment):
            adapter = adapters.get(state.provider)
            snapshot = checker(state.provider) if checker else (
                adapter.check_availability() if adapter else {"available": None, "reason": "adapter missing"})
            updated = observe(conn, state.provider, snapshot, account=state.account, now=moment)
            if updated.status == AVAILABLE:
                recovered.append(updated)
    return recovered


def observe(conn, provider: str, snapshot: dict, *, account="default", now=None) -> ProviderState:
    from . import manager_state
    with db.immediate_transaction(conn):
        previous = get_state(conn, provider, account)
        if previous.status != AVAILABLE:
            manager_state.capture_provider(conn, provider, previous.reason)
        return _observe(conn, provider, snapshot, account=account, now=now)


def _observe(conn, provider: str, snapshot: dict, *, account="default", now=None) -> ProviderState:
    moment = now or datetime.now(UTC)
    key = account_key(provider, account)
    if unmetered(provider):
        snapshot = {**snapshot, "complete": True, "windows": []}
    if snapshot.get("complete"):
        conn.execute("DELETE FROM quota_windows WHERE account_key=?", (key,))
    for window in snapshot.get("windows", []):
        reset = window.get("resets_at")
        if isinstance(reset, (int, float)):
            reset = datetime.fromtimestamp(reset, UTC).isoformat()
        conn.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_key,bucket,window) DO UPDATE SET used_percent=excluded.used_percent,resets_at=excluded.resets_at,observed_at=excluded.observed_at,source=excluded.source",
                     (key, window.get("bucket", provider), window["window"], window.get("used_percent"), reset, moment.isoformat(), snapshot.get("reason", "provider event")))
    blocking = [dict(r) for r in conn.execute("SELECT * FROM quota_windows WHERE account_key=? AND used_percent>=100", (key,))
                if not r["resets_at"] or (db.parse_ts(r["resets_at"]) or moment) > moment]
    if snapshot.get("available") is True and not blocking:
        return clear_cooldown(conn, provider, account, reason="availability confirmed by provider")
    resets = [db.parse_ts(w["resets_at"]) for w in blocking if w["resets_at"]]
    retry = max((r for r in resets if r), default=None) or db.parse_ts(snapshot.get("retry_at"))
    state = begin_cooldown(conn, provider, account=account,
                           reason=snapshot.get("reason", "availability unknown"), retry_at=retry)
    if snapshot.get("auth_error"):
        state.status = AUTH_REQUIRED
        state.retry_at = None
        _write(conn, state)
    return state


def next_retry(conn: sqlite3.Connection, now: datetime | None = None) -> int | None:
    """Seconds until the soonest cooldown ends, or None if nothing is cooling."""
    moment = now or datetime.now(UTC)
    waits = [
        s.seconds_remaining(moment) for s in list_states(conn) if s.status != AVAILABLE
    ]
    return min(waits) if waits else None


def _write(conn: sqlite3.Connection, state: ProviderState) -> None:
    from contextlib import nullcontext

    from . import manager_state
    with nullcontext(conn) if conn.in_transaction else db.immediate_transaction(conn):
        previous = get_state(conn, state.provider, state.account)
        if previous.status != AVAILABLE:
            manager_state.capture_provider(conn, state.provider, previous.reason)
        _persist_state(conn, state)
        if state.status != AVAILABLE:
            manager_state.capture_provider(conn, state.provider, state.reason, new=previous.status == AVAILABLE)


def _persist_state(conn, state: ProviderState) -> None:
    conn.execute(
        """INSERT INTO provider_state
               (account_key, provider, account, status, reason, detected_at,
                retry_at, raw_message, consecutive)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(account_key) DO UPDATE SET
               status = excluded.status, reason = excluded.reason,
               detected_at = excluded.detected_at, retry_at = excluded.retry_at,
               raw_message = excluded.raw_message, consecutive = excluded.consecutive""",
        (state.key, state.provider, state.account, state.status, state.reason,
         state.detected_at, state.retry_at, state.raw_message, state.consecutive),
    )
