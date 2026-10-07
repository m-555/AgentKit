"""Read-only presentation of reported account allowance; never derive it from tokens."""

from __future__ import annotations

import math
from datetime import UTC, datetime


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def reports(view: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    providers = {row["account_key"]: row for row in view.get("providers", [])}
    from .providers import unmetered
    windows = [row for row in view.get("quota_windows", [])
               if not unmetered(providers.get(row.get("account_key"), {}).get("provider", ""))]
    result = []
    for row in windows:
        provider = providers.get(row.get("account_key"), {})
        used = row.get("used_percent")
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            used = None
        observed = _stamp(row.get("observed_at"))
        reset = _stamp(row.get("resets_at"))
        stale = not observed or (now - observed).total_seconds() > 600 or observed > now
        due = bool(reset and reset <= now)
        result.append(
            {
                "provider": provider.get("provider", "unknown"),
                "account_key": row.get("account_key"),
                "bucket": row.get("bucket"),
                "window": row.get("window"),
                "used_percent": used,
                "remaining_percent": 100 - used if used is not None else None,
                "resets_at": row.get("resets_at"),
                "observed_at": row.get("observed_at"),
                "stale": stale,
                "reset_due": due,
                "status": provider.get("status", "UNKNOWN"),
                "source": "reported_account_window",
            }
        )
    covered = {row.get("account_key") for row in windows}
    for key, provider in providers.items():
        if key not in covered:
            result.append(
                {
                    "provider": provider.get("provider", "unknown"),
                    "account_key": key,
                    "status": provider.get("status", "UNKNOWN"),
                    "window": "not_applicable" if unmetered(provider.get("provider", "")) else "unreported",
                    "used_percent": None,
                    "remaining_percent": None,
                    "resets_at": provider.get("retry_at"),
                    "observed_at": provider.get("detected_at"),
                    "stale": True,
                    "reset_due": False,
                    "source": "unmetered_health" if unmetered(provider.get("provider", "")) else "availability_only",
                }
            )
    return result
