"""Native experimental transport receipts use the same durable recovery ledger."""
from __future__ import annotations

from . import db, wake_adapters
from . import recovery_store as store
from .locking import atomic_write


def arm(root, result):
    conn = db.connect(root)
    try:
        session_id = store.register(conn, identifier="codex-vscode:" + result["thread"],
            provider="codex", account="default", host="codex-vscode", role="manager",
            reference=result["thread"], identity={"pipe_peer": result["pipe_peer"], "owner": result["owner"]})
        proof = {key: result[key] for key in ("thread", "owner", "pipe_peer", "authorizing_turn")}
        intent = store.arm(conn, session_id, result["authorizing_turn"], proof,
                           "quota" if result["quota_recovery_only"] else "authorized_idle_diagnostic")
        result["recovery_intent_id"] = intent["id"]
    finally:
        conn.close()


def claim(root, result, *, now):
    conn = db.connect(root)
    try:
        intent = store.get(conn, result["recovery_intent_id"])
        quota = result["latest_quota"]
        evidence = {"observed_at": result["last_quota_observed_at"],
                    "available": quota["provider_confirmed_available"],
                    "complete": quota["complete"], "windows": quota["windows"]}
        # Native watcher has just rechecked pause, authorizing turn, peer/owner,
        # terminal idle state and provider evidence. It does not require a sleeping
        # manager's acknowledgement; the delivered turn must audit before working.
        packet = {"manager_checkpoint": str(root / ".ai/runtime/manager-checkpoint.json"),
                  "authorizing_turn": result["authorizing_turn"], "intent": intent["id"]}
        atomic_write(root / ".ai/runtime" / ("wake-audit-" + intent["id"] + ".json"), store.encode(packet))
        if not wake_adapters.ready(conn, intent["id"], evidence, authorized=True,
                                   old_owner_stopped=True, proof=intent["proof"], audit_packet=True, now=now):
            raise RuntimeError("Shared recovery policy rejected native wake")
        attempt = store.claim(conn, intent["id"], "native-one-shot-watcher", proof=intent["proof"],
                              authorization=intent["authorization"])
        if not attempt:
            raise RuntimeError("Native wake already claimed; reconcile instead of resubmitting")
        result["recovery_attempt_id"] = attempt
    finally:
        conn.close()


def save(root, result):
    identifier = result.get("recovery_intent_id")
    if not identifier:
        return
    conn = db.connect(root)
    try:
        current = store.get(conn, identifier)
        if not current or current["state"] in store.TERMINAL:
            return
        status = result["status"]
        attempt = result.get("recovery_attempt_id")
        if result.get("submitted_turn_id") and current["state"] == "CLAIMED":
            store.move(conn, identifier, "DELIVERY_ACCEPTED", "Native request accepted; completion unverified", attempt_id=attempt)
        after = result.get("after_snapshot") or {}
        if result.get("submitted_turn_id") and after.get("latest_turn_id") == result["submitted_turn_id"]:
            current = store.get(conn, identifier)
            if not current["started_at"]:
                store.move(conn, identifier, "TURN_STARTED", "Submitted native turn observed", attempt_id=attempt)
        if result.get("native_turn_completed"):
            store.move(conn, identifier, "RECOVERED", "Same native turn completed", attempt_id=attempt)
        elif status == "delivery_outcome_unknown_no_retry":
            store.move(conn, identifier, "RECONCILING", "Native delivery outcome unknown; no automatic retry", attempt_id=attempt)
        elif status == "cancelled_no_delivery":
            store.cancel(conn, identifier, result.get("error", "Native authorization changed"))
        elif status in ("native_delivery_rejected_no_retry", "delivered_turn_failed_or_interrupted", "deadline_reached_no_delivery"):
            store.move(conn, identifier, "NEEDS_USER_ACTION", status, attempt_id=attempt)
    finally:
        conn.close()
