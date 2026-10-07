"""Durable manager-outage epochs and barriers, shared by every launch/merge path."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

from . import db, jobs, repo


def root_for(conn) -> Path:
    filename = conn.execute("PRAGMA database_list").fetchone()[2]
    return Path(filename).resolve().parent.parent


def state(conn, job_id: str) -> dict:
    row = conn.execute("SELECT * FROM manager_state WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else {"job_id": job_id, "epoch": 0, "acknowledged_epoch": 0}


def pending(conn, job_id: str | None) -> bool:
    if not job_id:
        return False
    row = state(conn, job_id)
    return row["epoch"] != row["acknowledged_epoch"]


def _outage_heads(conn) -> tuple[str | None, str | None]:
    """Project and integration heads for an outage record; slow on a large checkout."""
    root = root_for(conn)
    from .manager_evidence import integration
    return repo.head_commit(root), integration(root).get("head")


def record_outage(conn, job_id: str, provider: str, reason: str, *, new=False) -> int:
    own_transaction = not conn.in_transaction
    if own_transaction and not new and pending(conn, job_id):
        return int(state(conn, job_id)["epoch"])  # Already open: no write lock needed.
    # Gather the Git evidence before taking the write lock when this call owns it.
    heads = _outage_heads(conn) if own_transaction else None
    with db.immediate_transaction(conn) if own_transaction else nullcontext(conn):
        previous = state(conn, job_id)
        if pending(conn, job_id) and not new:
            return int(previous["epoch"])
        epoch = int(previous["epoch"]) + 1
        authorized = [t["id"] for t in db.list_tasks(conn) if t.get("job_id") == job_id
                      and t["status"] in ("READY", "LEASED", "RUNNING", "VERIFYING", "REVIEW")]
        signatures = {str(t["id"]): launch_signature(conn, t) for t in db.list_tasks(conn) if t["id"] in authorized}
        head, combined_head = heads or _outage_heads(conn)
        conn.execute("INSERT INTO manager_state(job_id,epoch,acknowledged_epoch,outage_reason,outage_provider,"
                     "outage_started_at,authorized_tasks,updated_at,outage_head,outage_integration_head) VALUES(?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(job_id) DO UPDATE SET epoch=excluded.epoch,outage_reason=excluded.outage_reason,"
                     "outage_provider=excluded.outage_provider,outage_started_at=excluded.outage_started_at,"
                     "authorized_tasks=excluded.authorized_tasks,updated_at=excluded.updated_at,outage_head=excluded.outage_head,"
                     "outage_integration_head=excluded.outage_integration_head,audit_epoch=NULL,audit_digest=NULL,audit_at=NULL,audit_report=NULL",
                     (job_id, epoch, previous["acknowledged_epoch"], reason, provider, db.utcnow(),
                      json.dumps(authorized), db.utcnow(), head, combined_head))
        conn.execute("UPDATE manager_state SET authorized_signatures=? WHERE job_id=?", (json.dumps(signatures, sort_keys=True), job_id))
        db.log_event(conn, None, "manager_outage", cause=reason,
                     effect="integration and newly dependent launches await manager recovery audit",
                     detail={"job": job_id, "epoch": epoch, "provider": provider, "authorized_tasks": authorized})
        return epoch


def capture_provider(conn, provider: str, reason: str, *, new=False) -> None:
    """Called before availability can erase the observed outage."""
    root = root_for(conn)
    for row in conn.execute("SELECT id FROM jobs WHERE status NOT IN ('DONE','BLOCKED')").fetchall():
        try:
            job = jobs.load(root, row["id"])
        except (OSError, ValueError):
            continue
        pinned = job.get("coordinator_model") or {}
        manager_provider = pinned.get("provider") or job.get("coordinator")
        if manager_provider == provider:
            record_outage(conn, row["id"], provider, reason, new=new)


def capture_current(conn) -> None:
    from . import providers
    for account in providers.list_states(conn):
        if account.status != providers.AVAILABLE:
            capture_provider(conn, account.provider, account.reason)


def allows_launch(conn, task: dict) -> bool:
    job_id = task.get("job_id")
    if not pending(conn, job_id):
        return True
    row = state(conn, str(job_id))
    saved = json.loads(row.get("authorized_signatures") or "{}")
    return (task["id"] in json.loads(row.get("authorized_tasks") or "[]")
            and saved.get(str(task["id"])) == launch_signature(conn, task))


def require_clear(conn, job_id: str | None) -> None:
    capture_current(conn)
    if pending(conn, job_id):
        raise ValueError("manager recovery audit and acknowledgement of the current epoch are required")


def launch_signature(conn, task: dict) -> str:
    """Stable approved task shape, revision and exact dependency freshness."""
    keys = ("spec_hash", "kind", "role", "description", "expected_read", "expected_write", "owned_paths", "depends_on", "contract_version", "model_assignment", "gate_level", "acceptance")
    data: dict = {k: task.get(k) for k in keys}
    runtime = conn.execute("SELECT revision,planned_revision FROM jobs WHERE id=?", (task.get("job_id"),)).fetchone()
    data["job_revision"] = dict(runtime) if runtime else None
    data["dependencies"] = []
    deps = {str(d) for d in task.get("depends_on") or []}
    for other in db.list_tasks(conn):
        if str(other["id"]) in deps or str(other.get("spec_id")) in deps:
            data["dependencies"].append({k: other.get(k) for k in ("id", "spec_hash", "status", "last_commit", "contract_version")})
            if other.get("worktree"):
                data["dependencies"][-1]["head"] = repo.head_commit(other["worktree"])
    from .config import load_project
    data["model_policy"] = load_project(root_for(conn)).raw.get("model_policy")
    return json.dumps(data, sort_keys=True, default=str)
