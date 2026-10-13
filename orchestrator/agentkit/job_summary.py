"""A short manager-written job summary for workers (token efficiency #2).

Every worker brief carried the job's full user-request history (about 8k
characters in a pilot project) on every model turn. Each task already carries its own
spec, files and rules; the job context only orients the worker. The manager
writes a summary of at most LIMIT characters that records the revision it
covers. Workers receive it instead of the history while it is current; once the
user changes the job, the full history returns until the manager rewrites the
summary, so no user instruction is ever dropped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, jobs
from .locking import atomic_write, exclusive

LIMIT = 1500


def set_summary(root: str | Path, job_id: str, text: str, *, author: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("a worker summary needs text")
    if len(text) > LIMIT:
        raise ValueError(f"a worker summary is at most {LIMIT} characters; it has {len(text)}")
    with exclusive(root, "jobs"):
        job = jobs.load(root, job_id)
        job["worker_summary"] = {"text": text, "revision": job["revision"], "at": db.utcnow(), "by": author}
        atomic_write(jobs.path(root, job_id), json.dumps(job, indent=2) + "\n")
    jobs.amend(root, job_id, f"Worker summary set for revision {job['revision']} by {author} "
                             f"({len(text)} characters).")
    return job["worker_summary"]


def current(job: dict) -> str | None:
    """The summary text while it covers the job's current revision, else None."""
    summary = job.get("worker_summary") or {}
    return summary.get("text") if summary.get("revision") == job.get("revision") else None


def worker_context(project: Any, job_id: str | None, task: Any) -> str:
    """Job context for a worker: the current summary, or the full history (workflow.job_context)."""
    from . import workflow
    if job_id and workflow.enabled(project):
        job = jobs.load(project.root, job_id)
        summary = current(job)
        owner = task.get("job_id") if isinstance(task, dict) else getattr(task, "job_id", None)
        if summary and (not owner or owner == job_id):
            return json.dumps({"id": job["id"], "revision": job.get("revision"), "summary": summary,
                               "acceptance": job.get("acceptance") or []}, ensure_ascii=False)
    return workflow.job_context(project, job_id, task)
