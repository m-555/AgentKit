"""Human acceptance of an exact, verified staging preview; never publishes main."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import db, integrator, jobs, manager_state, processes, repo, review_policy, verification
from .config import load_project
from .locking import atomic_write, exclusive


def _operator_only():
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("workers and supervised controls cannot impersonate the user")


def preview(conn, project, job_id):
    job = jobs.load(project.root, job_id)
    runtime = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not runtime:
        raise ValueError("job has no authoritative runtime state")
    tasks = [task for task in db.list_tasks(conn) if task.get("job_id") == job_id]
    required = [task for task in tasks if task["status"] != "CANCELLED"]
    branch = integrator.integration_branch(project)
    work = next((Path(entry["worktree"]) for entry in repo.worktree_list(project.root)
                 if entry.get("branch") == "refs/heads/" + branch), None)
    head = repo.rev_parse(project.root, branch) if repo.branch_exists(project.root, branch) else ""
    reasons = []
    if not required or any(task["status"] != "DONE" for task in required):
        reasons.append("Every required implementation and tester task must finish.")
    if runtime["revision"] != job["revision"] or runtime["planned_revision"] != job["revision"]:
        reasons.append("The latest user request needs a current plan.")
    if manager_state.pending(conn, job_id):
        reasons.append("Manager recovery audit is pending.")
    if any(process["job_id"] == job_id for process in processes.owning(conn)):
        reasons.append("A session still owns this job.")
    if work is None or not head or work.resolve() == project.root.resolve():
        reasons.append("A separate integration preview is required.")
    checked = verification.cached(conn, project, work, "full") if work and head else None
    if not checked:
        reasons.append("The current clean preview needs passing combined checks.")
    proofs = []
    for task in required:
        row = conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (task["id"],)).fetchone()
        gate = db.cached_gate(conn, task["id"], task["gate_level"], row["head_sha"]) if row else None
        valid = (row and row["verdict"] == "PASS" and gate and gate["passed"] and head
                 and repo.is_ancestor(project.root, row["head_sha"], head))
        if not valid:
            reasons.append(f"Task {task['id']} lacks current integrated approval/check evidence.")
        proofs.append({"id": task["id"], "spec_hash": task.get("spec_hash"), "status": task["status"],
                       "commit": row["head_sha"] if row else None,
                       "reviewer": row["reviewer"] if row else None,
                       "gate": task["gate_level"], "passed": bool(valid)})
    try:
        review_policy.validate_graph(project, tasks)
    except ValueError as exc:
        reasons.append(str(exc))
    mode = job.get("workflow_review", review_policy.mode(project))
    if mode != review_policy.mode(project) and runtime["status"] != "DONE":
        reasons.append("The saved job review policy differs from project configuration.")
    payload = {"job_id": job_id, "revision": job["revision"], "head": head,
               "tasks": proofs, "review": mode, "acceptance": job["acceptance"],
               "requests": job["requests"], "check_signature": checked["signature"] if checked else None,
               "cancelled": [task["id"] for task in tasks if task["status"] == "CANCELLED"]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {"job_id": job_id, "revision": job["revision"], "head": head, "digest": digest,
            "status": runtime["status"], "review": mode, "ready": not reasons,
            "reasons": reasons, "preview_path": str(work) if work else None,
            "acceptance": job["acceptance"], "requests": job["requests"], "tasks": proofs,
            "cancelled_tasks": payload["cancelled"],
            "checks": {"passed": bool(checked), "summary": checked["summary"] if checked else None}}


def prepare(conn, project, job_id):
    """Code, not a planning turn, prepares a finished human-review feature."""
    if review_policy.mode(project) != "human":
        return False
    runtime = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    tasks = [task for task in db.list_tasks(conn) if task.get("job_id") == job_id]
    required = [task for task in tasks if task["status"] != "CANCELLED"]
    if not runtime or runtime["status"] not in ("ACTIVE", "AWAITING_USER"):
        return False
    if not required or any(task["status"] != "DONE" for task in required):
        return False
    try:
        manager_state.require_clear(conn, job_id)
        with exclusive(project.root, "integration"), exclusive(project.root, "jobs"):
            if runtime["planned_revision"] != jobs.load(project.root, job_id)["revision"]:
                return False
            work = integrator.integration_worktree(project)
            result = verification.run(conn, project, work, "full")
            if not result.passed:
                raise ValueError(result.summary())
            packet = preview(conn, project, job_id)
            if not packet["ready"]:
                raise ValueError("; ".join(packet["reasons"]))
            conn.execute("UPDATE jobs SET status='AWAITING_USER',last_error=NULL,updated_at=? WHERE id=?",
                         (db.utcnow(), job_id))
            return True
    except (ValueError, OSError) as exc:
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", ("preview: " + str(exc), job_id))
        return False


def queue(root):
    """Read-only view; old databases/configurations are never migrated by GET."""
    project = load_project(root)
    conn = db.connect_readonly(root)
    try:
        result = []
        for row in conn.execute("SELECT id FROM jobs WHERE status IN ('ACTIVE','AWAITING_USER','DONE') ORDER BY updated_at DESC LIMIT 200"):
            try:
                packet = preview(conn, project, row["id"])
            except (OSError, ValueError):
                continue
            if packet["status"] == "AWAITING_USER" or packet["ready"]:
                result.append(packet)
        return result
    finally:
        conn.close()


def decide(root, job_id, revision, head, digest, verdict, evidence):
    _operator_only()
    if type(revision) is not int or verdict not in ("PASS", "CHANGES"):
        raise ValueError("current revision and PASS or CHANGES are required")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
        raise ValueError("provide 1-4000 characters describing what you tested or want changed")
    project = load_project(root)
    with exclusive(root, "integration"), exclusive(root, "jobs"):
        conn = db.connect(root)
        try:
            manager_state.require_clear(conn, job_id)
            packet = preview(conn, project, job_id)
            if (packet["revision"], packet["head"], packet["digest"]) != (revision, head, digest):
                raise ValueError("preview changed; refresh and test the current version")
            if not packet["ready"] or packet["status"] not in (("ACTIVE", "AWAITING_USER", "DONE") if verdict == "CHANGES" else ("ACTIVE", "AWAITING_USER")):
                raise ValueError("preview is not ready for this decision")
            if verdict == "PASS" and packet["review"] != "human":
                raise PermissionError("this job uses AI acceptance; human PASS is not its configured path")
            with db.immediate_transaction(conn):
                manager_state.require_clear(conn, job_id)
                current = preview(conn, load_project(root), job_id)
                if (current["revision"], current["head"], current["digest"]) != (revision, head, digest) or not current["ready"]:
                    raise ValueError("preview changed during decision; refresh and test the current version")
                if verdict == "CHANGES":
                    job = jobs.load(root, job_id)
                    job["requests"].append({"text": evidence, "at": db.utcnow()})
                    job["revision"] += 1
                    # File is the durable revision authority. A crash after this
                    # write leaves a stale plan, which blocks launches/acceptance.
                    atomic_write(jobs.path(root, job_id), json.dumps(job, indent=2) + "\n")
                    conn.execute("UPDATE jobs SET revision=? WHERE id=?", (job["revision"], job_id))
                conn.execute("INSERT INTO human_acceptance(job_id,revision,head_sha,digest,verdict,evidence,created_at) "
                             "VALUES(?,?,?,?,?,?,?)", (job_id, revision, head, digest, verdict, db.redact(evidence), db.utcnow()))
                if verdict == "PASS":
                    conn.execute("UPDATE jobs SET status='DONE',completed_sha=?,last_error=NULL,updated_at=? WHERE id=?",
                                 (head, db.utcnow(), job_id))
                else:
                    conn.execute("UPDATE jobs SET status='PLANNING',completed_sha=NULL,next_check=NULL,updated_at=? WHERE id=?",
                                 (db.utcnow(), job_id))
                db.log_event(conn, None, "human_accepted" if verdict == "PASS" else "human_changes_requested",
                             cause=evidence, detail={"job": job_id, "head": head, "revision": revision})
            return {"job_id": job_id, "status": "DONE" if verdict == "PASS" else "PLANNING", "head": head}
        finally:
            conn.close()
