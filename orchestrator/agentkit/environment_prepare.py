"""Durable environment readiness and setup holds, separate from agent execution."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import db, environment_checks, gates, worktrees
from . import environment_profiles as profiles
from .environment_capacity import cache_root, reserve
from .locking import atomic_write, exclusive


def receipt_path(project, work, profile_name=None):
    key = hashlib.sha256(str(Path(work).resolve()).encode()).hexdigest()[:24]
    suffix = "-" + hashlib.sha256(profile_name.encode()).hexdigest()[:12] if profile_name else ""
    return project.root / ".ai/runtime/environments" / f"{key}{suffix}.json"


def _blocked(profile, key, previous, work):
    if previous.get("status") != "blocked" or previous.get("key") != key:
        return False
    if previous.get("category") == "DISK_FULL":
        required = profile["min_free_bytes"] + profile["reserve_bytes"]
        return shutil.disk_usage(work).free < max(required, previous.get("free_bytes", 0) + 1048576)
    return True


def hold(project, work, task=None, *, level=None):
    profile = profiles.select(project, task, level=level)
    key = profiles.fingerprint(work, profile)
    file = receipt_path(project, work, profile["name"])
    previous = json.loads(file.read_text()) if file.exists() else {}
    if _blocked(profile, key, previous, work):
        return previous.get("reason", "Environment setup requires explicit repair")
    return None


def repair(project, work):
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Only the host/operator may clear a setup hold")
    latest = receipt_path(project, work)
    for file in latest.parent.glob(latest.stem + "*.json"):
        value = json.loads(file.read_text())
        if value.get("status") == "blocked":
            value.update(status="repair_requested", repair_at=db.utcnow())
            atomic_write(file, json.dumps(value) + "\n")


def prepare(project, work, task=None, *, level=None, profile_name=None):
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Dependency setup belongs to host code")
    work = Path(work).resolve()
    profile = profiles.select(project, task, level=level, profile_name=profile_name)
    key = profiles.fingerprint(work, profile)
    path = receipt_path(project, work, profile["name"])
    with exclusive(project.root, "environment-" + path.stem):
        previous = json.loads(path.read_text()) if path.exists() else {}
        if _blocked(profile, key, previous, work):
            return gates.GateResult("worktree_setup", False,
                                    skipped_reason="Setup blocked: " + previous["reason"])
        observation = profiles.observed(work, profile)
        if (previous.get("status") == "ready" and previous.get("key") == key
                and observation is not None and observation == previous.get("installed")):
            checks = environment_checks.run(project, work, profile)
            if not checks.passed:
                previous.update(status="blocked", category="SETUP_FAILED",
                                reason=db.redact(checks.summary()), at=db.utcnow())
                atomic_write(path, json.dumps(previous) + "\n")
                atomic_write(receipt_path(project, work), json.dumps(previous) + "\n")
                return checks
            return gates.GateResult("worktree_setup", True, checks.results,
                                    skipped_reason="Reused prepared environment: " + profile["name"])
        value = {"worktree": str(work), "profile": profile["name"], "key": key,
                 "strategy": profile["strategy"], "status": "preparing", "at": db.utcnow(),
                 "attempts": previous.get("attempts", 0) + 1, "ai_calls": 0,
                 "reserve_bytes": profile["reserve_bytes"], "free_bytes": shutil.disk_usage(work).free}
        atomic_write(path, json.dumps(value) + "\n")
        started = time.monotonic()
        results = []
        reused = False
        try:
            from . import environment_snapshots as snapshots
            from . import environment_wheels as wheels
            eligible = snapshots.eligible(work, profile)
            cache = cache_root(project)
            with reserve(project, work, profile), exclusive(cache, "prepare-" + key):
                if eligible:
                    reused = snapshots.restore(project, work, key)
                elif wheels.eligible(profile):
                    reused = wheels.restore(project, work, profile, key)
                if profile.get("components"):
                    for component in profile["components"]:
                        ready = prepare(project, work, profile_name=component)
                        if not ready.passed:
                            raise RuntimeError(ready.summary())
                if not reused:
                    for command in profile["setup"]:
                        env = {**os.environ, **worktrees.shared_cache_env(project.root)}
                        command_result = gates._run_one(command, work, gates.DEFAULT_TIMEOUT, env=env)
                        results.append(command_result)
                        if not command_result.ok:
                            break
                result = gates.GateResult("worktree_setup", all(r.ok for r in results), results)
                installed = profiles.observed(work, profile)
                if result.passed and installed is None:
                    raise ValueError("Setup did not create the profile's required resources")
                if result.passed:
                    checks = environment_checks.run(project, work, profile)
                    results.extend(checks.results)
                    result = gates.GateResult("worktree_setup", checks.passed, results)
                if result.passed and profiles.fingerprint(work, profile) != key:
                    raise ValueError("Setup changed dependency inputs; readiness not certified")
                if result.passed and eligible and not reused:
                    snapshots.capture(project, work, key)
                if not result.passed:
                    raise RuntimeError(result.summary())
                if result.passed and wheels.eligible(profile) and not reused:
                    wheels.capture(project, work, profile, key)
                value.update(status="ready", installed=installed, reused_snapshot=reused)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            reason = str(error)
            category = "DISK_FULL" if (getattr(error, "errno", None) == 28
                         or "errno 28" in reason.lower()
                         or "no space left" in reason.lower()
                         or "not enough space" in reason.lower()) else "SETUP_FAILED"
            value.update(status="blocked", category=category, reason=db.redact(reason)[-4000:],
                         free_bytes=shutil.disk_usage(work).free,
                         remediation="Recover disk capacity or explicitly repair/change the setup profile")
            result = gates.GateResult("worktree_setup", False, results, skipped_reason=reason[-1000:])
        value["duration_s"] = round(time.monotonic() - started, 3)
        atomic_write(path, json.dumps(value) + "\n")
        atomic_write(receipt_path(project, work), json.dumps(value) + "\n")
        if task:
            conn = db.connect(project.root)
            try:
                db.log_event(conn, task["id"], "environment_ready" if result.passed else "environment_blocked",
                             detail={k: v for k, v in value.items() if k != "installed"})
                if result.passed:
                    marker = project.root / ".ai/runtime" / f"task-{task['id']}-setup"
                    atomic_write(marker, key)
            finally:
                conn.close()
        return result
