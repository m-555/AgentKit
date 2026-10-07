"""Durable, idempotent recovery claims shared by all providers and hosts."""
from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from . import db
from .secrets import redact

TERMINAL = {"RECOVERED", "CANCELLED", "NEEDS_USER_ACTION"}
DELIVERED = {"CLAIMED", "DELIVERY_ACCEPTED", "TURN_STARTED", "RECONCILING"}
TRANSITIONS = {
    "WAITING_AVAILABILITY": {"READY_TO_WAKE", "CANCELLED", "NEEDS_USER_ACTION"},
    "READY_TO_WAKE": {"WAITING_AVAILABILITY", "CLAIMED", "CANCELLED", "NEEDS_USER_ACTION"},
    "CLAIMED": {"DELIVERY_ACCEPTED", "TURN_STARTED", "RECONCILING", "CANCELLED", "NEEDS_USER_ACTION"},
    "DELIVERY_ACCEPTED": {"TURN_STARTED", "RECONCILING", "CANCELLED", "NEEDS_USER_ACTION"},
    "TURN_STARTED": {"RECOVERED", "RECONCILING", "CANCELLED", "NEEDS_USER_ACTION"},
    "RECONCILING": {"DELIVERY_ACCEPTED", "TURN_STARTED", "RECOVERED", "CANCELLED", "NEEDS_USER_ACTION"},
}


def transaction(conn):
    return nullcontext(conn) if conn.in_transaction else db.immediate_transaction(conn)


def encode(value):
    body = json.dumps(redact(value), sort_keys=True, default=str)
    if len(body) > 16000:
        raise ValueError("Recovery metadata exceeds 16000 characters; store a checkpoint reference")
    return body


def register(conn, *, identifier, provider, account, host, role, reference,
             policy="subscription", job_id=None, task_id=None, process_id=None, identity=None):
    if not identifier or not provider or not reference or role not in ("worker", "manager", "reviewer"):
        raise ValueError("Session needs identity, provider, reference and a supported role")
    if policy not in ("subscription", "unmetered"):
        raise ValueError("Explicit subscription or unmetered account policy required")
    fields = (identifier, provider, account, host, role, reference, job_id, task_id,
              process_id, policy, encode(identity or {}), db.utcnow())
    with transaction(conn):
        old = session(conn, identifier)
        if old and any(old[key] != value for key, value in zip(
                ("provider", "account", "host", "role", "reference"), fields[1:6], strict=True)):
            raise ValueError("Registered session identity is immutable")
        conn.execute("INSERT INTO recovery_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET identity=excluded.identity,updated_at=excluded.updated_at", fields)
    return identifier


def session(conn, identifier):
    row = conn.execute("SELECT * FROM recovery_sessions WHERE id=?", (identifier,)).fetchone()
    return dict(row) if row else None


def get(conn, identifier):
    row = conn.execute("SELECT * FROM recovery_intents WHERE id=?", (identifier,)).fetchone()
    if not row:
        return None
    value = dict(row)
    for field in ("proof", "evidence"):
        value[field] = json.loads(value[field])
    return value


def arm(conn, session_id, authorization, proof, failure):
    if not authorization or not session(conn, session_id):
        raise ValueError("Registered session and authorization revision required")
    with transaction(conn):
        existing = conn.execute("SELECT id FROM recovery_intents WHERE session_id=? AND authorization=?",
                                (session_id, authorization)).fetchone()
        if existing:
            return get(conn, existing[0])
        identifier, stamp = uuid.uuid4().hex, db.utcnow()
        conn.execute("INSERT INTO recovery_intents(id,session_id,authorization,proof,failure,state,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,'WAITING_AVAILABILITY',?,?)",
                     (identifier, session_id, authorization, encode(proof), failure, stamp, stamp))
        journal(conn, identifier, "WAITING_AVAILABILITY", failure)
        return get(conn, identifier)


def journal(conn, identifier, state, reason):
    conn.execute("INSERT INTO recovery_journal(intent_id,state,reason,at) VALUES(?,?,?,?)",
                 (identifier, state, str(redact(reason))[:1000], db.utcnow()))


def move(conn, identifier, state, reason, *, attempt_id=None, evidence=None):
    with transaction(conn):
        current = get(conn, identifier)
        if not current:
            raise ValueError("Unknown recovery intent")
        if attempt_id and attempt_id != current["attempt_id"]:
            raise PermissionError("Receipt belongs to another delivery attempt")
        if current["state"] == state:
            return current
        if state not in TRANSITIONS.get(current["state"], set()):
            raise ValueError(f"Illegal recovery transition {current['state']} -> {state}")
        stamp = db.utcnow()
        conn.execute("UPDATE recovery_intents SET state=?,reason=?,evidence=?,updated_at=? WHERE id=?",
                     (state, str(redact(reason))[:1000], encode(evidence or current["evidence"]), stamp, identifier))
        column = {"DELIVERY_ACCEPTED": "accepted_at", "TURN_STARTED": "started_at", "RECOVERED": "completed_at"}.get(state)
        if column:
            conn.execute(f"UPDATE recovery_intents SET {column}=COALESCE({column},?) WHERE id=?", (stamp, identifier))
        journal(conn, identifier, state, reason)
        return get(conn, identifier)


def claim(conn, identifier, owner, *, proof, authorization, lease_seconds=120, now=None):
    if not owner or not 15 <= lease_seconds <= 3600:
        raise ValueError("Claim needs owner and lease 15..3600 seconds")
    with transaction(conn):
        current = get(conn, identifier)
        if not current or current["state"] != "READY_TO_WAKE":
            return None
        if current["authorization"] != authorization or encode(current["proof"]) != encode(proof):
            move(conn, identifier, "CANCELLED", "Authorization or preserved work changed before claim")
            return None
        moment = now or datetime.now(UTC)
        attempt = uuid.uuid4().hex
        conn.execute("UPDATE recovery_intents SET attempt_id=?,claim_owner=?,claimed_at=?,claim_until=? WHERE id=?",
                     (attempt, owner, moment.isoformat(), (moment + timedelta(seconds=lease_seconds)).isoformat(), identifier))
        move(conn, identifier, "CLAIMED", "Exclusive delivery claim persisted")
        return attempt


def reconcile_expired(conn, *, now=None):
    moment = now or datetime.now(UTC)
    for row in conn.execute("SELECT id,claim_until FROM recovery_intents WHERE state IN ('CLAIMED','DELIVERY_ACCEPTED')").fetchall():
        until = db.parse_ts(row["claim_until"])
        if until and until <= moment:
            move(conn, row["id"], "RECONCILING", "Delivery lease expired; observe before any retry")


def cancel(conn, identifier, reason):
    if not reason.strip():
        raise ValueError("Cancellation reason required")
    current = get(conn, identifier)
    if current and current["state"] not in TERMINAL:
        return move(conn, identifier, "CANCELLED", reason)
    return current


def snapshot(conn, *, limit=100):
    rows = conn.execute("SELECT i.id,s.provider,s.account,s.host,s.role,s.reference,s.job_id,s.task_id,"
                        "s.policy,i.state,i.reason,i.failure,i.attempt_id,i.claimed_at,i.accepted_at,"
                        "i.started_at,i.completed_at,i.updated_at FROM recovery_intents i "
                        "JOIN recovery_sessions s ON s.id=i.session_id ORDER BY i.updated_at DESC LIMIT ?",
                        (min(500, max(1, limit)),)).fetchall()
    return [dict(row) for row in rows]
