"""The external manager chat's own turns keep its lease fresh (F29).

Claude Code runs AgentKit's Stop and PreCompact hooks in the manager chat too.
When the hook's session is the one the lease registered, a fresh lease is
renewed: a working chat needs no manual heartbeat, and its lease lapses one TTL
after its last turn, when it stopped at a usage limit or its user left. Then
the supervisor's coordinator takes over. A lapsed lease is never revived here;
the returning chat renews it explicitly and audits the outage.
"""
from __future__ import annotations

import os
from datetime import UTC, timedelta
from pathlib import Path

from . import db, manager


def renew(root: Path, session_id: str) -> list[str]:
    """Jobs whose fresh lease belongs to this session; their heartbeat is renewed."""
    if not session_id or os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        return []
    conn = db.connect(root)
    renewed = []
    try:
        with db.immediate_transaction(conn):
            rows = conn.execute("SELECT * FROM manager_leases WHERE session_ref=? AND released_at IS NULL",
                                (session_id,)).fetchall()
            for record in map(dict, rows):
                if manager.fresh(record) and manager.pid_alive(record["pid"]):
                    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?",
                                 (db.utcnow(), record["job_id"]))
                    renewed.append(record["job_id"])
    finally:
        conn.close()
    return renewed


def describe(root, job_id: str) -> str:
    """Who manages the job now, in one sentence for the view's chat."""
    conn = db.connect_readonly(Path(root))
    try:
        owner = manager.coordinator_owner(conn, job_id)
        record = manager.lease(conn, job_id)
    finally:
        conn.close()
    if owner:
        return (f"The automatic manager is working now (process {owner['id']}). It reads new messages between "
                "steps; any it misses start its next turn as soon as this one ends.")
    beat = db.parse_ts(record["heartbeat_at"]) if record else None
    if record and beat and manager.fresh(record):
        stamp = (beat + timedelta(seconds=record["ttl_seconds"])).astimezone(UTC).strftime("%H:%M UTC")
        return (f"The VS Code manager chat holds this job until about {stamp} unless it works again. It reads "
                "your messages on its next turn; after that time the automatic manager answers them.")
    return ("The automatic manager starts by itself whenever the job needs it, including after a usage-limit "
            "reset. A message you send starts it within a minute.")
