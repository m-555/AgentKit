"""Runtime tables for model policy, handoffs and manager recovery.

Kept beside `db.py` rather than inside it so that file does not keep growing.
Every statement is idempotent: `db.connect` applies this on each connection.
"""
from __future__ import annotations

import sqlite3

_TABLES = """
CREATE TABLE IF NOT EXISTS verification_cache (
 head_sha TEXT NOT NULL, signature TEXT NOT NULL, level TEXT NOT NULL,
 passed INTEGER NOT NULL, summary TEXT NOT NULL, checked_at TEXT NOT NULL,
 PRIMARY KEY(head_sha,signature,level)
);
CREATE TABLE IF NOT EXISTS human_acceptance (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
 head_sha TEXT NOT NULL, digest TEXT NOT NULL, verdict TEXT NOT NULL,
 evidence TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planner_events (
 job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, process_id INTEGER NOT NULL,
 provider_observed_at TEXT
);
CREATE TABLE IF NOT EXISTS model_triggers (
 task_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, generation INTEGER NOT NULL,
 source_provider TEXT NOT NULL DEFAULT '', source_model TEXT NOT NULL DEFAULT '',
 target_provider TEXT NOT NULL, target_model TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_state (
 job_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
 acknowledged_epoch INTEGER NOT NULL DEFAULT 0, outage_reason TEXT, outage_provider TEXT,
 outage_started_at TEXT, authorized_tasks TEXT NOT NULL DEFAULT '[]',
 audit_epoch INTEGER, audit_digest TEXT, audit_at TEXT, audit_report TEXT,
 ack_at TEXT, ack_by TEXT, ack_evidence TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_leases (
 job_id TEXT PRIMARY KEY, holder TEXT NOT NULL, token_hash TEXT NOT NULL,
 provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
 effort TEXT NOT NULL DEFAULT '', session_ref TEXT NOT NULL DEFAULT '', pid INTEGER,
 ttl_seconds INTEGER NOT NULL, takeover_grace INTEGER, acquired_at TEXT NOT NULL,
 heartbeat_at TEXT NOT NULL, released_at TEXT, release_reason TEXT, outage_recorded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS manager_checkpoints (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, holder TEXT NOT NULL, epoch INTEGER NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

#: Columns added to tables owned by `db.py`. Requested values come from the launch;
#: observed values only from provider events, so the two are never conflated.
_COLUMNS = {
    "manager_state": {"outage_head": "TEXT", "outage_integration_head": "TEXT", "authorized_signatures": "TEXT"},
    "tasks": {"model_assignment": "TEXT", "blocked_meta": "TEXT"},
    "processes": {"pid_identity": "TEXT", "child_pid_identity": "TEXT","requested_model": "TEXT", "requested_effort": "TEXT", "requested_profile": "TEXT",
                  "observed_model": "TEXT", "observed_effort": "TEXT", "model_verified": "INTEGER", "child_launch_state": "TEXT"},
}


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(_TABLES)
    from . import recovery_schema
    recovery_schema.apply(conn)
    for table, columns in _COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    for table, columns in _COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = set(columns) - present
        if missing:
            raise sqlite3.DatabaseError(f"Incomplete migration for {table}: {sorted(missing)}")
