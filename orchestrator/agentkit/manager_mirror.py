"""A checkpoint file is a convenience mirror; SQLite remains authoritative."""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .locking import atomic_write
from .secrets import redact


def save(conn, job_id, identifier, payload):
    root = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent.parent
    row = conn.execute("SELECT created_at FROM manager_checkpoints WHERE id=?", (identifier,)).fetchone()
    value = {"job": job_id, "saved_at": row[0], "checkpoint_id": identifier, **redact(payload)}
    body = json.dumps(value, indent=2, default=str) + "\n"
    try:
        # Job ids are validated by jobs.create; still refuse unsafe file components.
        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
            raise ValueError("Unsafe checkpoint mirror job identifier")
        atomic_write(root / ".ai/runtime/manager-checkpoints" / (job_id + ".json"), body)
        atomic_write(root / ".ai/runtime/manager-checkpoint.json", body)
    except (OSError, ValueError) as error:
        db.log_event(conn, None, "manager_checkpoint_mirror_failed", cause=type(error).__name__,
                     detail={"job": job_id, "checkpoint_id": identifier, "database_authoritative": True})
