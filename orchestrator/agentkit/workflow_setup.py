"""Operator-selected workflow/profile settings; configuration never starts jobs."""
from __future__ import annotations

import json
import os
from dataclasses import replace

import yaml

from . import db, jobs, policy, processes, review_policy
from .config import load_project
from .locking import atomic_write, exclusive

ROLES = ("backend-builder", "backend-tester", "frontend-builder", "frontend-tester")


def worker_slots(project):
    values = (project.raw.get("workflow") or {}).get("worker_slots", {})
    if not isinstance(values, dict) or set(values) - set(ROLES):
        raise ValueError("workflow.worker_slots needs exact builder/tester roles")
    if any(type(value) is not int or not 1 <= value <= 3 for value in values.values()):
        raise ValueError("each worker pool needs 1-3 slots")
    return values


def apply(root, profile, *, replan_unstarted=None):
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("only the operator can change the team profile")
    if not isinstance(profile, dict) or set(profile) - {"workflow", "model_policy", "max_workers"}:
        raise ValueError("profile supports workflow, model_policy and max_workers only")
    workflow = profile.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) - {"mode", "review", "worker_slots"}:
        raise ValueError("profile workflow supports mode, review and worker_slots only")
    if workflow.get("mode") != "separate-tasks" or workflow.get("review") not in ("human", "ai"):
        raise ValueError("choose separate-tasks and human or ai review")
    maximum = profile.get("max_workers", 0)
    if type(maximum) is not int or maximum < 0:
        raise ValueError("max_workers must be zero (unlimited) or positive")
    with exclusive(root, "supervisor"), exclusive(root, "scheduler"), exclusive(root, "integration"), exclusive(root, "jobs"):
        project = load_project(root)
        raw = dict(project.raw)
        raw.update(profile)
        raw["workflow"] = {**(project.raw.get("workflow") or {}), **workflow}
        candidate = replace(project, raw=raw)
        review_policy.mode(candidate)
        worker_slots(candidate)
        problems = policy.validate_project(candidate)
        if problems:
            raise ValueError("; ".join(problems))
        conn = db.connect(root)
        try:
            if processes.owning(conn):
                raise ValueError("stop and audit existing sessions before applying a profile")
            unfinished = conn.execute("SELECT id,status FROM jobs WHERE status != 'DONE'").fetchall()
            if unfinished and replan_unstarted is None:
                raise ValueError("choose the workflow before creating a job; current unfinished jobs keep their policy")
            if replan_unstarted is not None:
                _require_unstarted(conn, root, replan_unstarted, unfinished)
            from .run_limits import limits
            limits(candidate)
            if replan_unstarted is not None:
                job = jobs.load(root, replan_unstarted)
                job["revision"] += 1
                job["workflow_review"] = review_policy.mode(candidate)
                job["requests"].append({"text": "Operator selected a new team profile for this unstarted job. Rebuild the bounded task graph before activation; execution remains paused.", "at": db.utcnow()})
                raw["execution_paused"] = True
                with db.immediate_transaction(conn):
                    atomic_write(jobs.path(root, replan_unstarted), json.dumps(job, indent=2) + "\n")
                    conn.execute("UPDATE jobs SET revision=?,planned_revision=0,next_check=NULL WHERE id=?", (job["revision"], replan_unstarted))
                    atomic_write(project.root / ".ai/project.yaml", yaml.safe_dump(raw, sort_keys=False))
            else:
                atomic_write(project.root / ".ai/project.yaml", yaml.safe_dump(raw, sort_keys=False))
            db.log_event(conn, None, "workflow_configured", detail={"review": review_policy.mode(candidate)})
        finally:
            conn.close()
        return {"review": review_policy.mode(candidate), "execution_paused": raw.get("execution_paused") is True}


def import_job(root, request):
    if not isinstance(request, dict) or set(request) - {"id", "request", "acceptance", "coordinator"}:
        raise ValueError("job file supports id, request, acceptance and coordinator only")
    if not isinstance(request.get("id"), str) or not isinstance(request.get("request"), str):
        raise ValueError("job file needs an id and request text")
    acceptance = request.get("acceptance", [])
    if not isinstance(acceptance, list) or not acceptance or any(not isinstance(item, str) or not item.strip() for item in acceptance):
        raise ValueError("job file needs concrete acceptance criteria as a list of strings")
    if len(request["request"]) + sum(map(len, acceptance)) > 12000:
        raise ValueError("job request exceeds 12000 characters; split the job")
    return jobs.create(root, request["id"], request["request"], request.get("coordinator", "auto"), acceptance=acceptance)


def _require_unstarted(conn, root, job_id, unfinished):
    from . import manager_state
    if len(unfinished) != 1 or unfinished[0]["id"] != job_id or unfinished[0]["status"] != "PLANNING":
        raise ValueError("conversion requires one unstarted job in PLANNING")
    manager_state.require_clear(conn, job_id)
    if conn.execute("SELECT 1 FROM processes WHERE job_id=? LIMIT 1", (job_id,)).fetchone():
        raise ValueError("conversion cannot change a job with prior worker/control sessions")
    if conn.execute("SELECT 1 FROM leases WHERE released_at IS NULL LIMIT 1").fetchone():
        raise ValueError("conversion requires no outstanding file leases")
    if any(task["job_id"] == job_id and (task["generation"] != 0 or task["status"] not in ("PLANNED", "READY", "CANCELLED")) for task in db.list_tasks(conn)):
        raise ValueError("conversion cannot discard started or uncertain task work")
    jobs.load(root, job_id)  # Refuse an invalid/missing durable job before writes.
