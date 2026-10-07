"""Idempotent generic recovery tables; no provider-specific session columns."""
from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS recovery_sessions (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, account TEXT NOT NULL,
 host TEXT NOT NULL, role TEXT NOT NULL, reference TEXT NOT NULL,
 job_id TEXT, task_id INTEGER, process_id INTEGER, policy TEXT NOT NULL,
 identity TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_intents (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES recovery_sessions(id),
 authorization TEXT NOT NULL, proof TEXT NOT NULL, failure TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}',
 attempt_id TEXT UNIQUE, delivery_process INTEGER, claim_owner TEXT, claimed_at TEXT, claim_until TEXT,
 accepted_at TEXT, started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, UNIQUE(session_id,authorization)
);
CREATE TABLE IF NOT EXISTS recovery_journal (
 id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES recovery_intents(id),
 state TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_pending ON recovery_intents(state,updated_at);
"""


def apply(conn):
    conn.executescript(SQL)
