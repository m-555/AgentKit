"""Durable user intent. Original requests and corrections are append-only."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db
from .locking import atomic_write, exclusive


def path(root: str | Path, job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", job_id):
        raise ValueError("job id must be a lowercase slug")
    return Path(root) / ".ai" / "jobs" / f"{job_id}.json"


def load(root: str | Path, job_id: str) -> dict:
    data = json.loads(path(root, job_id).read_text(encoding="utf-8"))
    if data.get("id") != job_id:
        raise ValueError("job id does not match its file")
    return data


def create(root: str | Path, job_id: str, request: str, coordinator: str = "auto",
           *, acceptance: list[str] | None = None, reviewers: list[str] | None = None) -> dict:
    from . import adapters
    coordinator = adapters.canonical_name(coordinator)
    reviewers = [adapters.canonical_name(name) for name in reviewers] if reviewers else ["codex", "claude-code"]
    if any(adapters.get(name) is None for name in reviewers):
        raise ValueError("unknown reviewer adapter")
    if coordinator not in ("auto", "codex", "claude-code") or any(name not in ("codex", "claude-code") for name in reviewers):
        raise ValueError("control roles require Astra/Codex or Opus/Claude")
    if not request.strip():
        raise ValueError("a request and an installed adapter name are required")
    with exclusive(root, "jobs"):
        target = path(root, job_id)
        if target.exists():
            raise ValueError(f"job {job_id} already exists; append a correction instead")
        from .config import load_project
        from .review_policy import mode
        data = {"workflow_review": mode(load_project(root)), "id": job_id, "coordinator": coordinator, "coordinator_selection": coordinator, "revision": 1,
                "requests": [{"text": request, "at": db.utcnow()}],
                "acceptance": acceptance or [], "decisions": [],
                "reviewers": reviewers or [coordinator], "plan": ""}
        atomic_write(target, json.dumps(data, indent=2) + "\n")
        sync(root, data)
        return data


def sync(root: str | Path, job: dict) -> None:
    conn = db.connect(root)
    try:
        conn.execute("INSERT INTO jobs(id,revision,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, updated_at=excluded.updated_at",
                     (job["id"], job["revision"], db.utcnow()))
    finally:
        conn.close()


def amend(root: str | Path, job_id: str, text: str, *, user: bool = False) -> dict:
    if not text.strip():
        raise ValueError("empty job update")
    with exclusive(root, "jobs"):
        job = load(root, job_id)
        key = "requests" if user else "decisions"
        job[key].append({"text": text, "at": db.utcnow()})
        if user:
            job["revision"] += 1
        atomic_write(path(root, job_id), json.dumps(job, indent=2) + "\n")
        sync(root, job)
        if user:
            conn = db.connect(root)
            try:
                conn.execute("UPDATE jobs SET status='PLANNING', completed_sha=NULL WHERE id=?", (job_id,))
            finally:
                conn.close()
        return job


def packet(root: str | Path, job_id: str) -> str:
    job = load(root, job_id)
    return "## Durable job memory (authoritative user intent)\n" + json.dumps(job, indent=2)


def pin_coordinator(root, job_id, selection):
    with exclusive(root, "jobs"):
        job = load(root, job_id)
        if job.get("coordinator_model") and job["coordinator_model"] != selection.to_dict():
            raise ValueError("coordinator model is already pinned")
        job["coordinator"] = selection.provider
        job["coordinator_model"] = selection.to_dict()
        atomic_write(path(root, job_id), json.dumps(job, indent=2) + "\n")
        return job


def initial_model_rejected(root, conn, job_id):
    """Fallback is allowed only before any coordinator work has been accepted."""
    with exclusive(root, "jobs"):
        job = load(root, job_id)
        if job.get("coordinator_selection") != "auto" or job["decisions"]:
            return
        if conn.execute("SELECT 1 FROM tasks WHERE job_id=?", (job_id,)).fetchone():
            return
        if conn.execute("SELECT 1 FROM processes WHERE job_id=? AND purpose='coordinator' AND status='FINISHED'", (job_id,)).fetchone():
            return
        job.pop("coordinator_model", None)
        job["coordinator"] = "auto"
        atomic_write(path(root, job_id), json.dumps(job, indent=2) + "\n")
        conn.execute("UPDATE jobs SET coordinator_session=NULL,next_check=NULL WHERE id=?", (job_id,))
