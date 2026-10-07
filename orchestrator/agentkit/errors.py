"""Telling apart the six ways a worker can stop badly.

They look identical at the process level — a non-zero exit and some text — and
they demand opposite responses:

    USAGE_LIMIT      the account's allowance is spent; wait hours, cost nothing
    RATE_LIMIT       transient throttling; retry in seconds or minutes
    PROVIDER_OUTAGE  the service is down; retry with backoff
    AUTH_ERROR       credentials are wrong; a human must fix it
    TASK_ERROR       the work itself failed; this one *is* the task's fault
    CRASH            something unclassifiable died

Only `TASK_ERROR` and `CRASH` may consume an attempt. Charging a task for its
provider's outage is how a healthy task eventually gets marked NEEDS_REPLAN —
which is exactly the bug this module exists to prevent, and which the previous
implementation had, because a Claude usage-limit message matched none of its
patterns and fell through to CRASH.

Generic patterns live here; anything provider-specific belongs in that provider's
adapter, so a change in Claude's or Codex's wording is a one-file change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

USAGE_LIMIT = "USAGE_LIMIT"
RATE_LIMIT = "RATE_LIMIT"
PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
AUTH_ERROR = "AUTH_ERROR"
TASK_ERROR = "TASK_ERROR"
CRASH = "CRASH"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
BUDGET = "BUDGET"
NONE = "NONE"

#: Classes that are the provider's problem, never the task's.
PROVIDER_CLASSES = (USAGE_LIMIT, RATE_LIMIT, PROVIDER_OUTAGE, AUTH_ERROR)

#: Classes that cool the whole account down rather than just retrying.
COOLDOWN_CLASSES = (USAGE_LIMIT, RATE_LIMIT, PROVIDER_OUTAGE)


@dataclass
class Classification:
    kind: str
    reason: str = ""
    raw: str = ""
    retry_at: datetime | None = None
    matched: str = ""

    @property
    def is_provider_problem(self) -> bool:
        return self.kind in PROVIDER_CLASSES

    @property
    def consumes_attempt(self) -> bool:
        return self.kind in (TASK_ERROR, CRASH)

    @property
    def cools_provider(self) -> bool:
        return self.kind in COOLDOWN_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "reason": self.reason, "matched": self.matched,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "raw": self.raw[:500],
        }


#: Ordered: usage limits are checked before rate limits, because a usage-limit
#: message often contains the word "limit" and must not be mistaken for one.
GENERIC_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (MODEL_UNAVAILABLE, "requested model unavailable", re.compile(
        r"(?i)model[_ -]not[_ -]found|invalid[_ -]model|\bmodel\b.{0,100}(?:does not exist|not available|not supported|not found|do not have access|don't have access)"
        r"|(?:do not|don't) have access to.{0,30}model|unknown model")),
    (USAGE_LIMIT, "subscription allowance exhausted", re.compile(
        r"(?i)\b(?:usage|plan|weekly|daily|session|message)\s+limit\s+(?:reached|exceeded|hit)"
        r"|\blimit\s+will\s+reset\b"
        r"|\byou(?:'ve| have)\s+(?:reached|used|hit)\s+(?:your|the)\s+(?:(?:usage|weekly|session)\s+)?limit\b"
        r"|\bout\s+of\s+(?:credits|messages)\b"
        r"|\bupgrade\s+to\s+continue\b")),
    (AUTH_ERROR, "credentials rejected", re.compile(
        r"(?i)\b(?:401|403)\b|\bunauthori[sz]ed\b|\bauthentication\s+(?:failed|error)\b"
        r"|\binvalid\s+api\s+key\b|\bnot\s+logged\s+in\b|\bplease\s+run\s+.{0,12}login\b")),
    (RATE_LIMIT, "throttled", re.compile(
        r"(?i)\b429\b|\brate[\s_-]?limit(?:ed|ing)?\b|\btoo\s+many\s+requests\b"
        r"|\bslow\s+down\b|\bretry[- ]after\b")),
    (PROVIDER_OUTAGE, "provider unavailable", re.compile(
        r"(?i)\b(?:500|502|503|504)\b|\boverloaded\b|\bservice\s+unavailable\b"
        r"|\bbad\s+gateway\b|\bupstream\s+(?:error|connect)\b|\bapi[_\s]error\b"
        r"|\becconnreset\b|\betimedout\b|\benotfound\b|\bconnection\s+(?:reset|refused)\b"
        r"|\bnetwork\s+error\b")),
    (BUDGET, "spend cap reached", re.compile(
        r"(?i)\bbudget\b.{0,20}\b(?:exceeded|reached|exhausted)\b|\bmax[-_ ]budget\b")),
)


def classify(
    text: str,
    exit_code: int | None = None,
    *,
    extra_patterns: tuple[tuple[str, str, re.Pattern[str]], ...] = (),
) -> Classification:
    """Classify a worker's failure output.

    `extra_patterns` are the calling adapter's provider-specific rules, checked
    *before* the generic ones so a vendor can be more precise about its own
    wording without the generic layer getting in first.
    """
    raw = text or ""
    blob = raw.replace("\\u2019", "'").replace(chr(0x2019), "'").replace(chr(0x2018), "'")
    for patterns in (extra_patterns, GENERIC_PATTERNS):
        for kind, reason, pattern in patterns:
            match = pattern.search(blob)
            if match:
                return Classification(
                    kind=kind, reason=reason, raw=raw, matched=match.group(0)[:120]
                )

    if exit_code in (None, 0):
        return Classification(kind=NONE, reason="clean exit", raw=raw)
    return Classification(kind=CRASH, reason=f"exit code {exit_code}", raw=raw)


def with_retry_at(result: Classification) -> Classification:
    """Attach a parsed reset time, if the message carries one."""
    from .providers import parse_retry_at

    if result.kind in COOLDOWN_CLASSES:
        result.retry_at = parse_retry_at(result.raw)
    return result
