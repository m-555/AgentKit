"""What each agent session was asked to run, and what its provider says it ran.

`requested_*` comes from the launch. `observed_*` comes only from provider
events (Claude's `system/init` reports its model; Codex `exec --json` currently
reports neither model nor effort). A session is shown as verified only when
the provider itself reported a matching model; otherwise it is "requested,
unverified", never "measured".

`describe` is the read-only view used by status displays: launch arguments are
redacted, and only the model, profile and effort variables are taken from the
environment. No transcript, reasoning or prompt text is returned.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db
from .secrets import redact_text

VIEW_ENV = ("AGENTKIT_MODEL", "AGENTKIT_MODEL_PROFILE", "AGENTKIT_MODEL_EFFORT", "AGENTKIT_TASK",
            "AGENTKIT_JOB", "AGENTKIT_GENERATION")


def model_matches(requested: str | None, observed: str | None) -> bool:
    """Same model, allowing a dated snapshot or context-size suffix of the same ID."""
    if not requested or not observed:
        return False
    want = requested.strip().lower()
    raw = observed.strip().lower()
    if raw == want:
        return True
    got = re.sub(r"\[\d+(?:k|m)\]$", "", raw)
    return got == want or bool(re.fullmatch(re.escape(want) + r"-\d{8}", got))


def observe(conn: sqlite3.Connection, process: dict[str, Any], detail: dict[str, Any]) -> str | None:
    """Record a provider-reported model/effort. Returns a failure text on substitution."""
    model = detail.get("model")
    effort = detail.get("effort")
    if not model and not effort:
        return None
    requested_model = process.get("requested_model") or _env(process).get("AGENTKIT_MODEL")
    requested_effort = process.get("requested_effort") or _env(process).get("AGENTKIT_MODEL_EFFORT")
    fields: dict[str, Any] = {}
    if model:
        fields["observed_model"] = str(model)
    if effort:
        fields["observed_effort"] = str(effort)
    mismatch = None
    if model and requested_model and not model_matches(requested_model, str(model)):
        mismatch = f"pinned model {requested_model} not available: provider reported {model}"
    elif effort and requested_effort and str(effort).lower() != str(requested_effort).lower():
        mismatch = f"pinned model {requested_model} not available at {requested_effort} effort: provider reported {effort}"
    fields["model_verified"] = 0 if mismatch else (1 if model and requested_model else None)
    conn.execute("UPDATE processes SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                 (*fields.values(), process["id"]))
    db.log_event(conn, process.get("task_id"), "model_mismatch" if mismatch else "model_observed",
                 cause=mismatch or f"provider reported {model or '-'} / {effort or '-'}",
                 detail={"process": process["id"], "requested_model": requested_model,
                         "requested_effort": requested_effort, "observed_model": model, "observed_effort": effort})
    return mismatch


def _env(process: dict[str, Any]) -> dict[str, str]:
    try:
        return dict(json.loads(process.get("launch_json") or "{}").get("env") or {})
    except (TypeError, ValueError):
        return {}


def verification(process: dict[str, Any]) -> str:
    if process.get("model_verified") == 1:
        return "verified by provider event"
    if process.get("model_verified") == 0:
        return "MISMATCH reported by provider"
    return "requested; provider did not report the model"


def describe(process: dict[str, Any]) -> dict[str, Any]:
    """A secret-free, reasoning-free view of one session for read-only dashboards."""
    try:
        launch = json.loads(process.get("launch_json") or "{}")
    except (TypeError, ValueError):
        launch = {}
    env = launch.get("env") or {}
    return {
        "id": process.get("id"), "purpose": process.get("purpose"), "provider": process.get("provider"),
        "task_id": process.get("task_id"), "job_id": process.get("job_id"), "status": process.get("status"),
        "generation": process.get("generation"), "pid": process.get("pid"), "child_pid": process.get("child_pid"),
        "started_at": process.get("started_at"), "heartbeat_at": process.get("heartbeat_at"),
        "ended_at": process.get("ended_at"), "exit_code": process.get("exit_code"),
        "requested_model": process.get("requested_model") or env.get("AGENTKIT_MODEL"),
        "requested_profile": process.get("requested_profile") or env.get("AGENTKIT_MODEL_PROFILE"),
        "requested_effort": process.get("requested_effort") or env.get("AGENTKIT_MODEL_EFFORT"),
        "observed_model": process.get("observed_model"), "observed_effort": process.get("observed_effort"),
        "model_verification": verification(process),
        "has_session": bool(process.get("session_token")),
        "cwd": launch.get("cwd"),
        "argv": [redact_text(str(arg)) for arg in (launch.get("argv") or [])],
        "env": {k: env[k] for k in VIEW_ENV if k in env},
        "error": redact_text(str(process.get("error") or ""))[-500:],
    }


def listing(conn: sqlite3.Connection, *, active_only: bool = False, task_id: int | None = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if active_only:
        clauses.append("status IN ('STARTING','RUNNING')")
    if task_id is not None:
        clauses.append("task_id=?")
        params.append(task_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"SELECT * FROM processes{where} ORDER BY id", params).fetchall()
    return [describe(dict(r)) for r in rows]
