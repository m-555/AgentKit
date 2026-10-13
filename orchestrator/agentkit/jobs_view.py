"""The Jobs tab: what waits for your reply, and each job's state, for the repository shown."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db, jobs, problems


def read(root) -> dict:
    root = Path(root)
    try:
        conn = db.connect_readonly(root)
    except (OSError, sqlite3.Error):
        return {"problems": [], "jobs": []}
    try:
        runtime = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM jobs")}
        counts: dict[str, dict[str, int]] = {}
        for task in db.list_tasks(conn):
            if task.get("job_id"):
                per = counts.setdefault(task["job_id"], {})
                per[task["status"]] = per.get(task["status"], 0) + 1
        waiting = problems.listing(conn)
    finally:
        conn.close()
    rows = []
    for path in (root / ".ai" / "jobs").glob("*.json"):
        try:
            job = jobs.load(root, path.stem)
        except (OSError, ValueError):
            continue
        state = runtime.get(job["id"], {})
        rows.append({"id": job["id"], "status": state.get("status") or "PLANNING", "revision": job["revision"],
                     "planned_revision": state.get("planned_revision"), "last_error": state.get("last_error"),
                     "request": (job["requests"][0]["text"] if job["requests"] else "")[:400],
                     "updates": max(0, len(job["requests"]) - 1), "tasks": counts.get(job["id"], {}),
                     "screenshots": sum(len(r.get("attachments") or []) for r in job["requests"]),
                     "updated_at": state.get("updated_at")})
    rows.sort(key=lambda row: str(row["updated_at"] or ""), reverse=True)
    rows.sort(key=lambda row: row["status"] == "DONE")  # unfinished jobs first, newest first within each
    return {"problems": waiting, "jobs": rows}


def open_count(root) -> int:
    """How many problems wait for the user; 0 when unknown."""
    try:
        conn = db.connect_readonly(root)
    except (OSError, sqlite3.Error):
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) FROM problems WHERE status='OPEN'").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
