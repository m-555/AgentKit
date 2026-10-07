"""Host capabilities and common readiness policy; never execute arbitrary launch data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import db
from . import recovery_store as store


class WakeAdapter(Protocol):
    def capability(self) -> dict: ...
    def deliver(self, intent: dict, attempt_id: str) -> dict: ...
    def observe(self, intent: dict) -> dict: ...


@dataclass(frozen=True)
class HostAdapter:
    host: str
    support: str
    reason: str

    def capability(self):
        return {"host": self.host, "support": self.support, "reason": self.reason}

    def deliver(self, intent, attempt_id):
        # These hosts deliver from their existing guarded runtime, not a second launcher.
        return {"accepted": False, "reason": self.reason, "attempt_id": attempt_id}

    def observe(self, intent):
        return {"state": intent["state"], "attempt_id": intent["attempt_id"]}


REGISTRY: dict[str, WakeAdapter] = {
    "cli": HostAdapter("cli", "SUPPORTED", "Delivery uses the guarded AgentKit scheduler/monitor"),
    "codex-vscode": HostAdapter("codex-vscode", "EXPERIMENTAL", "Private native bridge; persistent quota recovery requires host registration and real-cycle qualification"),
    "claude-editor": HostAdapter("claude-editor", "UNSUPPORTED", "No qualified editor wake interface is registered"),
}


def adapter(host):
    return REGISTRY.get(host, HostAdapter(host, "UNSUPPORTED", "No qualified wake adapter is registered"))


def ready(conn, identifier, evidence, *, authorized, old_owner_stopped, proof,
          host_healthy=True, audit_packet=True, now=None):
    """Both roles/hosts use fresh account evidence and exact authorization before claim."""
    current = store.get(conn, identifier)
    if not current or current["state"] not in ("WAITING_AVAILABILITY", "READY_TO_WAKE"):
        return False
    session = store.session(conn, current["session_id"])
    if not authorized or store.encode(proof) != store.encode(current["proof"]):
        store.cancel(conn, identifier, "User pause/cancel, revision change or preserved bytes changed")
        return False
    support = adapter(session["host"]).capability()
    if support["support"] == "UNSUPPORTED":
        store.move(conn, identifier, "NEEDS_USER_ACTION", support["reason"])
        return False
    stamp = db.parse_ts(evidence.get("observed_at"))
    moment = now or datetime.now(UTC)
    fresh = bool(stamp and moment - timedelta(seconds=300) <= stamp <= moment + timedelta(seconds=5))
    if session["policy"] == "unmetered":
        available = evidence.get("healthy") is True
    else:
        windows = evidence.get("windows") or []
        available = evidence.get("available") is True and evidence.get("complete") is True
        available = available and all(isinstance(w.get("used_percent"), (int, float))
                                      and not isinstance(w.get("used_percent"), bool)
                                      and 0 <= w["used_percent"] < 100 for w in windows)
    qualified = fresh and available and old_owner_stopped and host_healthy
    qualified = qualified and (session["role"] != "manager" or audit_packet)
    state = "READY_TO_WAKE" if qualified else "WAITING_AVAILABILITY"
    store.move(conn, identifier, state,
               "Fresh provider/host evidence; authorization and ownership checked" if qualified
               else "Waiting for fresh availability, host health, stopped owner or manager audit packet",
               evidence={**evidence, "support": support})
    return bool(qualified)


def deliver(conn, identifier, host, *, proof, authorization, owner):
    """Dispatch once; receipt loss requires observation, never a blind second call."""
    attempt = store.claim(conn, identifier, owner, proof=proof, authorization=authorization)
    if not attempt:
        return None
    try:
        receipt = host.deliver(store.get(conn, identifier), attempt)
        state = "DELIVERY_ACCEPTED" if receipt.get("accepted") is True else "NEEDS_USER_ACTION"
        store.move(conn, identifier, state, receipt.get("reason", "Wake adapter receipt"),
                   attempt_id=attempt, evidence=receipt)
    except Exception as error:
        store.move(conn, identifier, "RECONCILING", f"Delivery outcome unknown: {type(error).__name__}",
                   attempt_id=attempt)
    return attempt


def observe(conn, identifier, host):
    current = store.get(conn, identifier)
    if not current or current["state"] in store.TERMINAL or not current["attempt_id"]:
        return current
    receipt = host.observe(current)
    if receipt.get("attempt_id") != current["attempt_id"]:
        raise PermissionError("Observation belongs to another delivery")
    if receipt.get("started") is True and not current["started_at"]:
        current = store.move(conn, identifier, "TURN_STARTED", "Actual turn start observed",
                             attempt_id=current["attempt_id"], evidence=receipt)
    if receipt.get("completed") is True and current["started_at"]:
        state = "RECOVERED" if receipt.get("success") is True else "NEEDS_USER_ACTION"
        current = store.move(conn, identifier, state, "Actual turn completion observed",
                             attempt_id=current["attempt_id"], evidence=receipt)
    return current
