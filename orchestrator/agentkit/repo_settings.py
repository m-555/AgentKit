"""The settings a manager changes, read and applied per repository for the view.

The view shows one repository's settings and saves them while that repository is
stopped: its project.yaml for roles, models, efforts, slots and run options, and the
job's manager pin through the regular operator re-pin. Nothing here starts sessions;
`repo_controls.start` does that when the user presses Start.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import yaml

from . import db, jobs, manager, models, policy, processes, providers, review_policy, run_limits
from .config import load_project
from .locking import atomic_write, exclusive

PROVIDERS = ("codex", "claude-code", "local-opencode")
WORKER_ROLES = ("backend-builder", "backend-tester", "frontend-builder", "frontend-tester")
PROFILE_SWITCHES = ("sol", "opus", "sonnet", "qwen")
KEYS = {"manager", "reviewer", "review", "assignments", "worker_slots", "max_workers",
        "visible_windows", "max_turns", "profiles", "mode", "job_tokens"}
MODES = ("feature", "separate-tasks")


def operator_only() -> None:
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("only the operator can change repository settings or start a repository")


def _choice(entry) -> dict:
    return {"profile": entry.name, "model": entry.model, "effort": entry.effort}


def _unfinished(conn) -> list[str]:
    return [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status != 'DONE' ORDER BY updated_at DESC")]


def running(root, conn) -> str | None:
    """Why settings cannot change now, or None when the repository is stopped."""
    from .service_control import status
    service = status(root)
    if service["guardian_alive"] or service["supervisor_alive"]:
        return "Stop this repository before changing its settings."
    if conn is not None and processes.owning(conn):
        return "Agents of this repository are still running; wait for them to finish before changing settings."
    return None


def _stopped(conn) -> list[dict]:
    """Tasks stopped on a tool, permission or setup problem (infra_stop)."""
    from .infra_stop import meta, stopped
    return [{"task": t["id"], "title": t["title"], "job": t.get("job_id"), "stage": meta(t)["stage"],
             "reason": t.get("blocker") or ""} for t in stopped(conn)]


def _manager(root, conn, job_id) -> dict:
    job = jobs.load(root, job_id)
    record = manager.lease(conn, job_id)
    lease = None
    if record and not record["released_at"]:
        stamp = db.parse_ts(record["heartbeat_at"])
        lease = {"holder": record["holder"], "fresh": manager.fresh(record),
                 "expires_at": (stamp + timedelta(seconds=record["ttl_seconds"])).isoformat() if stamp else None}
    return {"job": job_id, "pin": job.get("coordinator_model"), "lease": lease}


def read(root) -> dict:
    root = Path(root)
    project = load_project(root)
    raw = project.raw
    try:
        conn = db.connect_readonly(root)  # showing settings never creates or migrates state
    except FileNotFoundError:
        conn = None  # onboarded but never run: no jobs, leases or provider records yet
    try:
        coordinator, reviewer = policy.role(project, "coordinator"), policy.role(project, "reviewer")
        default = _choice(models.profile(project, models.CONTROL[0], control=True))
        assignments = {}
        for name in (policy._settings(project).get("assignments") or {}):
            assignment = policy.named(project, name)
            assignments[name] = {**_choice(assignment.primary), "roles": list(assignment.roles),
                                 "fallbacks": [_choice(f.profile) for f in assignment.fallbacks]}
        switches = policy._settings(project).get("profiles") or {}
        from .service_control import status
        return {
            "root": str(root.resolve()), "name": root.name, "service": status(root), "blocked": running(root, conn),
            "execution_paused": raw.get("execution_paused") is True,
            "stopped": _stopped(conn) if conn is not None else [],
            "managers": [_manager(root, conn, job_id) for job_id in _unfinished(conn)] if conn else [],
            "manager": _choice(coordinator.primary) if coordinator else default,
            "reviewer": _choice(reviewer.primary) if reviewer else default,
            "review": review_policy.mode(project),
            "mode": (raw.get("workflow") or {}).get("mode") or "legacy",
            "assignments": assignments,
            "worker_slots": dict((raw.get("workflow") or {}).get("worker_slots") or {}),
            "max_workers": raw.get("max_workers", 0),
            "visible_windows": raw.get("visible_windows") is True,
            "max_turns": run_limits.limits(project).get("max_turns"),
            "job_tokens": (raw.get("budgets") or {}).get("job_tokens"),
            "profiles": {name: (switches.get(name) or {}).get("enabled", True) is not False for name in PROFILE_SWITCHES},
            "providers": [(providers.get_state(conn, name) if conn else providers.ProviderState(
                provider=name, account="default", status=providers.AVAILABLE)).to_dict() for name in PROVIDERS],
            "choices": {"control": list(models.CONTROL), "worker": list(policy.WORKER_PROFILES),
                        "control_efforts": list(policy.CONTROL_EFFORTS), "worker_efforts": list(policy.EFFORTS),
                        "models": {name: models.PROFILES[name].model for name in PROFILE_SWITCHES},
                        "worker_roles": list(WORKER_ROLES)},
        }
    finally:
        if conn is not None:
            conn.close()


def _entry(current: dict | None, requested, *, control: bool) -> dict:
    if not isinstance(requested, dict) or set(requested) - {"profile", "effort"}:
        raise ValueError("a model choice needs profile and effort only")
    name = str(requested.get("profile") or "")
    allowed = models.CONTROL if control else policy.WORKER_PROFILES
    if name not in allowed:
        raise ValueError(f"choose one of {', '.join(allowed)}")
    keep = current.get("model") if current and current.get("profile") == name else None
    entry = {"profile": name, "model": keep or models.PROFILES[name].model}
    if models.PROFILES[name].provider != "local-opencode":
        entry["effort"] = requested.get("effort")
    return entry


def _merge(raw: dict, changes: dict) -> dict:
    raw = json.loads(json.dumps(raw))
    settings = raw.get("model_policy") or {}
    raw["model_policy"] = settings
    if "manager" in changes or "reviewer" in changes:
        roles = settings.setdefault("roles", {})
        for key, role in (("manager", "coordinator"), ("reviewer", "reviewer")):
            if key in changes:
                roles[role] = _entry(roles.get(role), changes[key], control=True)
    if "assignments" in changes:
        existing = settings.get("assignments") or {}
        if not isinstance(changes["assignments"], dict) or set(changes["assignments"]) - set(existing):
            raise ValueError("only existing model assignments can be changed here")
        for name, requested in changes["assignments"].items():
            current = existing[name]
            existing[name] = {**{k: v for k, v in current.items() if k not in ("profile", "model", "effort")},
                              **_entry(current, requested, control=False)}
    if "profiles" in changes:
        if not isinstance(changes["profiles"], dict) or set(changes["profiles"]) - set(PROFILE_SWITCHES) or \
                any(type(value) is not bool for value in changes["profiles"].values()):
            raise ValueError(f"profiles take true or false for {', '.join(PROFILE_SWITCHES)}")
        switches = settings.setdefault("profiles", {})
        for name, on in changes["profiles"].items():
            switches[name] = {**(switches.get(name) or {}), "enabled": on}
    workflow = raw.get("workflow") or {}
    if "mode" in changes:
        if changes["mode"] not in MODES:
            raise ValueError(f"workflow is one of {', '.join(MODES)}")
        workflow["mode"] = changes["mode"]
    if "review" in changes:
        workflow["review"] = changes["review"]
    if "worker_slots" in changes:
        if not isinstance(changes["worker_slots"], dict) or set(changes["worker_slots"]) - set(WORKER_ROLES):
            raise ValueError("worker slots are per builder/tester role")
        workflow["worker_slots"] = {**(workflow.get("worker_slots") or {}), **changes["worker_slots"]}
    if workflow:
        raw["workflow"] = workflow
    if "max_workers" in changes:
        if type(changes["max_workers"]) is not int or changes["max_workers"] < 0:
            raise ValueError("max workers must be zero (unlimited) or positive")
        raw["max_workers"] = changes["max_workers"]
    if "visible_windows" in changes:
        if type(changes["visible_windows"]) is not bool:
            raise ValueError("visible windows is on or off")
        raw["visible_windows"] = changes["visible_windows"]
    if "job_tokens" in changes:
        value = changes["job_tokens"]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("the job token budget is a positive whole number, or empty for none")
        budgets = {k: v for k, v in (raw.get("budgets") or {}).items() if k != "job_tokens"}
        raw["budgets"] = {**budgets, **({"job_tokens": value} if value else {})}
    if "max_turns" in changes:
        raw["execution_limits"] = {**(raw.get("execution_limits") or {}), "max_turns": changes["max_turns"]}
    return raw


def _pin_for(project, wanted) -> dict:
    """The job pin a re-pin to `wanted` records: the profile at the chosen effort."""
    return {**models.profile(project, wanted.name, control=True).to_dict(), "effort": wanted.effort}


def apply(root, changes: dict) -> dict:
    """Save settings for a stopped repository; a changed manager re-pins its unfinished jobs."""
    operator_only()
    if not isinstance(changes, dict) or not changes or set(changes) - KEYS:
        raise ValueError(f"settings support {', '.join(sorted(KEYS))}")
    root = Path(root)
    with exclusive(root, "supervisor"), exclusive(root, "scheduler"), exclusive(root, "integration"):
        project = load_project(root)
        raw = _merge(project.raw, changes)
        candidate = replace(project, raw=raw)
        review_policy.mode(candidate)
        from .workflow_setup import worker_slots
        worker_slots(candidate)
        run_limits.limits(candidate)
        problems = policy.validate_project(candidate)
        if problems:
            raise ValueError("; ".join(problems))
        conn = db.connect(root)
        try:
            reason = running(root, conn)
            if reason:
                raise PermissionError(reason)
            repinned = []
            if "manager" in changes:
                from .manager_repin import repin
                chosen = policy.role(candidate, "coordinator")
                if chosen is None:
                    raise ValueError("choose a manager model")
                wanted = chosen.primary
                for job_id in _unfinished(conn):
                    if jobs.load(root, job_id).get("coordinator_model") != _pin_for(candidate, wanted):
                        repin(conn, root, job_id, wanted.name,
                              f"The operator chose {wanted.model} ({wanted.effort}) as manager in the AgentKit view.",
                              effort=wanted.effort)
                        repinned.append(job_id)
            atomic_write(root / ".ai" / "project.yaml", yaml.safe_dump(raw, sort_keys=False))
            db.log_event(conn, None, "settings_changed", cause="operator saved settings in the AgentKit view",
                         detail={"changed": sorted(changes), "repinned": repinned})
        finally:
            conn.close()
    return {"saved": sorted(changes), "repinned": repinned}
