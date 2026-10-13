"""The view's Agents tab: recent agent sessions, working ones first (read-only)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db
from .windows import speaker

LIMIT = 40
LIVE = ("STARTING", "RUNNING")


def agents(root) -> list[dict]:
    """Builders, testers, reviewers and automatic managers, newest first with working ones on top."""
    try:
        conn = db.connect_readonly(Path(root))
    except (OSError, ValueError, sqlite3.Error):
        return []
    try:
        rows = conn.execute("SELECT p.*, t.title AS task_title, t.role AS task_role FROM processes p "
                            "LEFT JOIN tasks t ON t.id=p.task_id ORDER BY p.id DESC LIMIT ?", (LIMIT,)).fetchall()
    finally:
        conn.close()
    found = []
    for row in map(dict, rows):
        role = row["task_role"] if row["purpose"] == "worker" and row.get("task_role") else row["purpose"]
        found.append({"id": row["id"], "speaker": speaker(role), "role": role, "task_id": row.get("task_id"),
                      "title": row.get("task_title") or "", "job_id": row.get("job_id") or "",
                      "provider": row.get("provider") or "",
                      "model": row.get("observed_model") or row.get("requested_model") or "",
                      "status": row.get("status") or "", "running": row.get("status") in LIVE,
                      "started_at": row.get("started_at"), "ended_at": row.get("ended_at")})
    return sorted(found, key=lambda agent: (not agent["running"], -agent["id"]))
