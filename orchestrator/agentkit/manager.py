"""External manager bridge: leased ownership, heartbeats and durable memory.

An external chat is not resurrected. Its bridge PID and lease fence autonomous
coordinators; after confirmed death the CLI coordinator loads saved job memory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from . import db, jobs, manager_state, models, processes
from .reconcile import pid_alive
from .secrets import redact


def lease(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM manager_leases WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def fresh(record: dict, now=None) -> bool:
    stamp = db.parse_ts(record["heartbeat_at"])
    return bool(not record["released_at"] and stamp and
                stamp + timedelta(seconds=record["ttl_seconds"]) > (now or datetime.now(UTC)))


def blocks_spawn(conn, job_id: str) -> bool:
    record = lease(conn, job_id)
    if not record:
        return False
    if fresh(record) or not record["pid"] or pid_alive(record["pid"]):
        return True
    if not record["outage_recorded"]:
        manager_state.record_outage(conn, job_id, record["provider"], "external manager lease ended; bridge process confirmed stopped")
        conn.execute("UPDATE manager_leases SET outage_recorded=1 WHERE job_id=?", (job_id,))
    return False


def attach(conn, root, job_id: str, holder: str, pid: int, *, ttl_seconds=90, selection=None, session_ref="") -> str:
    from .config import load_project
    if not holder.strip() or not 15 <= ttl_seconds <= 3600 or not pid_alive(pid):
        raise ValueError("attachment requires holder, a live bridge PID and TTL 15..3600 seconds")
    job = jobs.load(root, job_id)
    project = load_project(root)
    candidates = models.control_candidates(project, job, "coordinator")
    chosen = selection or candidates[0]
    if chosen not in candidates:
        raise PermissionError("external manager selection differs from authorized coordinator model")
    with db.immediate_transaction(conn):
        if blocks_spawn(conn, job_id) or any(p["purpose"] == "coordinator" and p["job_id"] == job_id for p in processes.owning(conn)):
            raise ValueError("a manager or live control process already owns the job")
        jobs.pin_coordinator(root, job_id, chosen)
        token = secrets.token_urlsafe(32)
        now = db.utcnow()
        conn.execute("INSERT INTO manager_leases(job_id,holder,token_hash,provider,model,effort,session_ref,pid,ttl_seconds,acquired_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(job_id) DO UPDATE SET holder=excluded.holder,token_hash=excluded.token_hash,provider=excluded.provider,model=excluded.model,effort=excluded.effort,"
                     "session_ref=excluded.session_ref,pid=excluded.pid,ttl_seconds=excluded.ttl_seconds,acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,released_at=NULL,release_reason=NULL,outage_recorded=0",
                     (job_id, holder, hashlib.sha256(token.encode()).hexdigest(), chosen.provider, chosen.model, chosen.effort, session_ref, pid, ttl_seconds, now, now))
        db.log_event(conn, None, "external_manager_attached", detail={"job": job_id, "holder": holder, "pid": pid, "model": chosen.to_dict()})
        if session_ref:
            from . import adapters, recovery_store
            runtime = adapters.get(chosen.provider, project)
            host = "codex-vscode" if chosen.provider == "codex" and os.environ.get("CODEX_THREAD_ID") == session_ref else "claude-editor" if chosen.provider == "claude-code" else "external"
            recovery_store.register(conn, identifier=host + ":" + session_ref, provider=chosen.provider,
                account=runtime.account_id() if runtime else "default", host=host, role="manager",
                reference=session_ref, job_id=job_id, policy="unmetered" if getattr(runtime, "allowance_policy", None) == "unmetered" else "subscription",
                identity={"pid":pid,"holder":holder})
        return token


def handover(conn, job_id: str, token: str, holder: str, pid: int, *, session_ref: str,
             ttl_seconds: int = 90) -> str:
    """Move this job's lease to the user's next manager session; returns its new credential.

    The current credential proves the caller holds the lease, so the previous
    chat's bridge need not still be running. Pin and recovery epoch are
    unchanged and the lease never lapses, so no CLI coordinator can be spawned
    in between; the previous credential stops working.
    """
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("a supervised worker or coordinator cannot take over the manager lease")
    if not holder.strip() or not session_ref.strip() or not 15 <= ttl_seconds <= 3600 or not pid_alive(pid):
        raise ValueError("handover requires holder, session reference, a live bridge PID and TTL 15..3600 seconds")
    with db.immediate_transaction(conn):
        record = lease(conn, job_id)
        if (not record or record["released_at"] or
                not hmac.compare_digest(record["token_hash"], hashlib.sha256(token.encode()).hexdigest())):
            raise PermissionError("invalid external manager lease credential")
        new_token = secrets.token_urlsafe(32)
        conn.execute("UPDATE manager_leases SET holder=?,token_hash=?,session_ref=?,pid=?,ttl_seconds=?,"
                     "heartbeat_at=?,outage_recorded=0 WHERE job_id=?",
                     (holder, hashlib.sha256(new_token.encode()).hexdigest(), session_ref, pid, ttl_seconds,
                      db.utcnow(), job_id))
        db.log_event(conn, None, "external_manager_handover",
                     detail={"job": job_id, "from": record["holder"], "to": holder, "pid": pid,
                             "model": {"provider": record["provider"], "model": record["model"],
                                       "effort": record["effort"]}})
    return new_token


def require_lease(conn, job_id: str, token: str, *, allow_expired=False) -> dict:
    record = lease(conn, job_id)
    if not record or not hmac.compare_digest(record["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
        raise PermissionError("invalid external manager lease credential")
    if record["released_at"] or not pid_alive(record["pid"]) or (not allow_expired and not fresh(record)):
        raise PermissionError("external manager lease expired, released or bridge process stopped")
    return record


def heartbeat(conn, job_id: str, token: str) -> None:
    with db.immediate_transaction(conn):
        record = require_lease(conn, job_id, token, allow_expired=True)
        if not fresh(record):
            manager_state.record_outage(conn, job_id, record["provider"], "external manager heartbeat expired; authenticated holder renewed")
        conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (db.utcnow(), job_id))


def checkpoint(conn, job_id: str, token: str, payload: dict) -> int:
    with db.immediate_transaction(conn):
        record = require_lease(conn, job_id, token)
        allowed = {"decisions", "completed", "remaining", "tests", "next_action", "blockers"}
        if not isinstance(payload, dict) or set(payload) - allowed or not payload.get("next_action"):
            raise ValueError("manager checkpoint needs next_action and only decisions/completed/remaining/tests/blockers")
        encoded = json.dumps(redact(payload), default=str)
        if len(encoded) > 100000:
            raise ValueError("manager checkpoint is too large")
        row = conn.execute("INSERT INTO manager_checkpoints(job_id,holder,epoch,payload,created_at) VALUES(?,?,?,?,?)",
                           (job_id, record["holder"], manager_state.state(conn, job_id)["epoch"], encoded, db.utcnow()))
        identifier = int(row.lastrowid)
    from .manager_mirror import save
    save(conn, job_id, identifier, payload)
    return identifier


def release(conn, job_id: str, token: str, reason: str = "external manager released ownership") -> None:
    with db.immediate_transaction(conn):
        record = require_lease(conn, job_id, token, allow_expired=True)
        conn.execute("UPDATE manager_leases SET released_at=?,release_reason=? WHERE job_id=?", (db.utcnow(), str(redact(reason)), job_id))
        manager_state.record_outage(conn, job_id, record["provider"], reason)


def packet(conn, job_id: str) -> dict:
    state = manager_state.state(conn, job_id)
    row = conn.execute("SELECT * FROM manager_checkpoints WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return {"recovery": state, "external_checkpoint": json.loads(row["payload"]) if row else None}
