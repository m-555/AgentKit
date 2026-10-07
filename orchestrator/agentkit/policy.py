"""Explicit model assignments: exact pins and approved fallbacks only.

Without an explicit policy AgentKit keeps its ranked defaults (Astra/Opus for
control, Sol then Opus for workers). An explicit entry changes the contract:

* the named profile, model and effort are used exactly, with no catalog upgrade
  when `model` is given and no silent provider substitution;
* a worker may move only to a listed fallback, and only for the failure classes
  listed in that fallback's `when` (default: `USAGE_LIMIT`);
* the coordinator has no fallback: its provider never changes.

    model_policy:
      roles:
        coordinator: {profile: sol, model: gpt-6.1-sol, effort: high}
        reviewer:    {profile: sol, model: gpt-6.1-sol, effort: high}
      assignments:
        backend:
          profile: opus
          model: claude-opus-5-5
          effort: high
          fallback:
            - {profile: sol, model: gpt-6.1-sol, effort: high, when: [USAGE_LIMIT]}
        frontend: {profile: sol, model: gpt-6.1-sol, effort: high}
      default_assignment: null

Tasks opt in with `model_assignment: backend`, or an inline mapping with the
same keys. A plain `model_profile` stays a legacy preference.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from . import db
from .models import HIGH_RISK, PROFILES, Profile, enabled, profile

TRIGGERS = ("USAGE_LIMIT", "RATE_LIMIT", "PROVIDER_OUTAGE", "AUTH_ERROR", "MODEL_UNAVAILABLE", "CRASH")
DEFAULT_TRIGGERS = ("USAGE_LIMIT",)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CONTROL_PROFILES = ("astra", "sol", "opus")
CONTROL_EFFORTS = ("medium", "high", "xhigh", "max")
WORKER_PROFILES = ("sol", "opus", "sonnet", "qwen")
_ENTRY_KEYS = {"profile", "model", "effort", "fallback"}
_FALLBACK_KEYS = {"profile", "model", "effort", "when"}


@dataclass(frozen=True)
class Fallback:
    profile: Profile
    when: tuple[str, ...]


@dataclass(frozen=True)
class Assignment:
    name: str
    primary: Profile
    fallbacks: tuple[Fallback, ...] = ()

    def candidates(self, trigger: str | set[str] | None = None) -> list[Profile]:
        active = {trigger} if isinstance(trigger, str) else set(trigger or ())
        return [self.primary, *[f.profile for f in self.fallbacks if active & set(f.when)]]

    def allows(self, selected: Profile) -> bool:
        return selected == self.primary or any(selected == f.profile for f in self.fallbacks)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "primary": self.primary.to_dict(),
                "fallback": [{**f.profile.to_dict(), "when": list(f.when)} for f in self.fallbacks]}


def _settings(project) -> dict[str, Any]:
    raw = (getattr(project, "raw", {}) or {}).get("model_policy") or {}
    if not isinstance(raw, dict):
        raise ValueError("model_policy must be a mapping")
    return raw


def _entry(project, data: Any, where: str, *, control: bool, keys=_ENTRY_KEYS) -> Profile:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be a mapping with profile and effort")
    unknown = set(data) - keys
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    name = str(data.get("profile") or "")
    allowed = CONTROL_PROFILES if control else WORKER_PROFILES
    if name not in allowed:
        raise ValueError(f"{where}: profile must be one of {', '.join(allowed)}")
    if not enabled(project, name):
        raise ValueError(f"{where}: profile {name} is disabled in model_policy.profiles")
    effort = data.get("effort")
    if PROFILES[name].provider == "local-opencode":
        if effort is not None:
            raise ValueError(f"{where}: the local model has no effort setting")
    elif effort not in (CONTROL_EFFORTS if control else EFFORTS):
        choices = CONTROL_EFFORTS if control else EFFORTS
        raise ValueError(f"{where}: effort must be explicit and one of {', '.join(choices)}")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError(f"{where}: model must be a non-empty model ID")
    base = profile(project, name)
    # An explicit model ID is used verbatim: no catalog upgrade, no alias rewrite.
    return replace(base, model=model or base.model, effort=effort)


def _fallbacks(project, data: Any, where: str, primary: Profile, *, control: bool) -> tuple[Fallback, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ValueError(f"{where}.fallback must be a list")
    result: list[Fallback] = []
    for index, raw in enumerate(data):
        label = f"{where}.fallback[{index}]"
        item = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(item, dict):
            # YAML 1.1 reads a bare `on:` key as boolean True; accept it as `when`.
            for alias in (True, "on"):
                if alias in item and "when" not in item:
                    item["when"] = item.pop(alias)
        selected = _entry(project, item, label, control=control, keys=_FALLBACK_KEYS)
        on = item.get("when", list(DEFAULT_TRIGGERS))
        if not isinstance(on, list) or not on or any(t not in TRIGGERS for t in on):
            raise ValueError(f"{label}.when must list failure classes from {', '.join(TRIGGERS)}")
        if (selected.provider, selected.model) == (primary.provider, primary.model) or any(
                (selected.provider, selected.model) == (f.profile.provider, f.profile.model) for f in result):
            raise ValueError(f"{label} repeats a model already in this assignment")
        result.append(Fallback(selected, tuple(on)))
    return tuple(result)


def _assignment(project, data: Any, name: str, where: str, *, control: bool = False) -> Assignment:
    primary = _entry(project, data, where, control=control)
    fallbacks = _fallbacks(project, data.get("fallback"), where, primary, control=control)
    return Assignment(name, primary, fallbacks)


def role(project, name: str) -> Assignment | None:
    """The explicit coordinator or reviewer policy, or None for legacy defaults."""
    if name not in ("coordinator", "reviewer"):
        raise ValueError(f"unknown control role {name}")
    data = (_settings(project).get("roles") or {}).get(name)
    if data is None:
        return None
    result = _assignment(project, data, f"role:{name}", f"model_policy.roles.{name}", control=True)
    if result.fallbacks:
        # Control roles have no durable failure trigger to approve a switch against,
        # so a pinned coordinator or reviewer waits for its own model instead.
        raise ValueError(f"model_policy.roles.{name} cannot declare a fallback; a pinned control "
                         "role waits for its own model and provider")
    return result


def named(project, name: str) -> Assignment:
    assignments = _settings(project).get("assignments") or {}
    if not isinstance(assignments, dict) or name not in assignments:
        raise ValueError(f"unknown model assignment {name!r}; define it under model_policy.assignments")
    return _assignment(project, assignments[name], name, f"model_policy.assignments.{name}")


def parse_task_value(value: Any) -> str | dict | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("model_assignment must be an assignment name or a mapping")
    if not value.lstrip().startswith("{"):
        return value
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("inline model_assignment must be a mapping")
    return loaded


def validate_task_shape(value: Any) -> None:
    """Checks that need no project file; `for_task` completes validation."""
    value = parse_task_value(value)
    if isinstance(value, str) and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError(f"invalid model_assignment name: {value!r}")
    if isinstance(value, dict) and (set(value) - _ENTRY_KEYS or "profile" not in value):
        raise ValueError("inline model_assignment needs profile and effort, plus optional model and fallback")


def for_task(project, task: dict[str, Any]) -> Assignment | None:
    """The pinned assignment for a worker task, or None when it uses the defaults."""
    value = parse_task_value(task.get("model_assignment"))
    if value is None and not task.get("model_profile"):
        from .workflow import enabled
        if enabled(project) and task.get("role") in (_settings(project).get("assignments") or {}):
            value = task["role"]  # User-selected role assignment; no planner model-ID copying.
    if value is None:
        value = _settings(project).get("default_assignment")
        if value is None:
            return None
    result = named(project, value) if isinstance(value, str) else _assignment(
        project, value, "task", f"task {task.get('spec_id') or task.get('id')} model_assignment")
    for selected in (result.primary, *[f.profile for f in result.fallbacks]):
        if selected.name in ("sonnet", "qwen") and (
                task.get("complexity", "standard") != "easy" or task.get("kind") in HIGH_RISK):
            raise ValueError(f"{selected.name} in {result.name} requires an easy task without a high-risk kind")
        if selected.name == "qwen" and task.get("kind") != "RESEARCH":
            raise ValueError("qwen assignments are limited to RESEARCH tasks")
    return result


def validate_project(project) -> list[str]:
    """Every configuration problem, so one edit can fix them all."""
    problems: list[str] = []
    try:
        settings = _settings(project)
    except ValueError as exc:
        return [str(exc)]
    for name in ("coordinator", "reviewer"):
        try:
            role(project, name)
        except ValueError as exc:
            problems.append(str(exc))
    assignments = settings.get("assignments") or {}
    if not isinstance(assignments, dict):
        problems.append("model_policy.assignments must be a mapping")
        assignments = {}
    for name in assignments:
        try:
            named(project, str(name))
        except ValueError as exc:
            problems.append(str(exc))
    default = settings.get("default_assignment")
    if default is not None and default not in assignments:
        problems.append(f"model_policy.default_assignment {default!r} is not a defined assignment")
    return problems


def control_allowed(project, role_name: str, selected: Profile, pinned: dict | None = None) -> bool:
    """Whether `selected` may run a control role. Fails closed.

    Precedence: a job's persisted coordinator pin, then the explicit role entry,
    then the configured Astra/Opus defaults (high). A later policy edit can neither
    switch nor invalidate an existing pin, and an explicit entry never falls
    through to the legacy list.
    """
    if pinned:
        return selected == Profile(**pinned)
    explicit = role(project, role_name)
    if explicit is not None:
        return selected == explicit.primary
    from .models import CONTROL, profile
    return selected in [profile(project, name, control=True) for name in CONTROL]


def coordinator_pin(project, task: dict[str, Any]) -> dict | None:
    """The persisted coordinator model of the task's job, read from durable job memory."""
    job_id = task.get("job_id")
    if not job_id:
        return None
    from . import jobs
    try:
        return jobs.load(project.root, str(job_id)).get("coordinator_model")
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------- failure triggers


def record_trigger(conn: sqlite3.Connection, task_id: int, kind: str, provider: str = "", model: str = "") -> None:
    """Remember why the last worker stopped; fallbacks are approved per failure class."""
    conn.execute("INSERT INTO model_triggers(task_id,kind,provider,model,at) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(task_id) DO UPDATE SET kind=excluded.kind,provider=excluded.provider,"
                 "model=excluded.model,at=excluded.at", (task_id, kind, provider or "", model or "", db.utcnow()))


def trigger(conn: sqlite3.Connection, task_id: int) -> str | None:
    row = conn.execute("SELECT kind FROM model_triggers WHERE task_id=?", (task_id,)).fetchone()
    return str(row["kind"]) if row else None


def clear_trigger(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("DELETE FROM model_triggers WHERE task_id=?", (task_id,))


def provider_trigger(conn: sqlite3.Connection, provider: str) -> str | None:
    """Why an account is unavailable, in failure-class terms, or None when it is usable.

    Exhausted quota windows are authoritative; otherwise the recorded cooldown
    reason is mapped conservatively, and anything unrecognised counts as an outage
    rather than as quota (which is the class fallbacks are usually approved for).
    """
    from . import providers
    state = providers.get_state(conn, provider)
    if state.status == providers.AVAILABLE:
        return None
    if state.status == providers.AUTH_REQUIRED:
        return "AUTH_ERROR"
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    windows = conn.execute("SELECT resets_at FROM quota_windows WHERE account_key=? AND used_percent>=100",
                           (state.key,))
    if any(not row["resets_at"] or (db.parse_ts(row["resets_at"]) or now) > now for row in windows):
        return "USAGE_LIMIT"
    reason = f"{state.reason} {state.raw_message}".lower()
    if any(word in reason for word in ("allowance", "usage limit", "quota", "weekly", "plan limit")):
        return "USAGE_LIMIT"
    if "throttl" in reason or "rate" in reason:
        return "RATE_LIMIT"
    return "PROVIDER_OUTAGE"


def active_triggers(conn: sqlite3.Connection, project, task: dict[str, Any]) -> set[str]:
    """The task's own last failure plus the current reason its primary is unavailable."""
    found = set()
    if task.get("id"):
        recorded = trigger(conn, int(task["id"]))
        if recorded:
            found.add(recorded)
    assigned = for_task(project, task)
    if assigned is not None:
        current = provider_trigger(conn, assigned.primary.provider)
        if current:
            found.add(current)
    return found


def describe(project) -> dict[str, Any]:
    settings = _settings(project)
    roles = {}
    for name in ("coordinator", "reviewer"):
        explicit = role(project, name)
        roles[name] = explicit.to_dict() if explicit else "default (Astra, then Opus, high)"
    return {"roles": roles,
            "assignments": {n: named(project, n).to_dict() for n in (settings.get("assignments") or {})},
            "default_assignment": settings.get("default_assignment")}
