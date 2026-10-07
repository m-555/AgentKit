"""Explicit role pins and approved worker fallbacks; no paid inference."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    adapters,
    db,
    errors,
    jobs,
    models,
    planning,
    policy,
    processes,
    providers,
    quota,
    reviews,
    scheduler,
    spec,
)
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import ProjectConfig

SOL = {"profile": "sol", "model": "gpt-6.1-sol", "effort": "xhigh"}
OPUS = {"profile": "opus", "model": "claude-opus-5-5", "effort": "xhigh"}
PILOT = {"model_policy": {
    "roles": {"coordinator": dict(SOL), "reviewer": dict(SOL)},
    "assignments": {
        "backend": {**OPUS, "fallback": [{**SOL, "when": ["USAGE_LIMIT"]}]},
        "frontend": dict(SOL),
    },
}}


def capable():
    result = {}
    for name in ("codex", "claude-code"):
        caps = CapabilitySet(adapter=name)
        for key in caps.values:
            caps.set(key, True)
        result[name] = caps
    return result


@pytest.fixture
def state(tmp_path):
    conn = db.connect(tmp_path)
    yield conn, ProjectConfig(root=tmp_path, raw={k: v for k, v in PILOT.items()})
    conn.close()


def task(conn, assignment, **fields):
    identifier = db.create_task(conn, title="work", status="READY", model_assignment=assignment,
                                expected_write=["src/x.py"], **fields)
    return db.get_task(conn, identifier)


def test_no_explicit_policy_keeps_legacy_control_and_worker_defaults(tmp_path):
    project = ProjectConfig(root=tmp_path)
    job = jobs.create(tmp_path, "legacy", "Keep defaults")
    assert [(p.name, p.effort) for p in models.control_candidates(project, job, "review")] == [
        ("astra", "high"), ("opus", "high")]
    assert [p.name for p in models.worker_candidates(project, {"kind": "SAFE_PARALLEL"})] == ["sol", "opus"]
    assert policy.validate_project(project) == []


def test_explicit_sol_reviewer_and_coordinator_are_exact(state, monkeypatch):
    conn, project = state
    job = jobs.create(project.root, "pilot", "Build it", "codex")
    expected = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)
    assert models.control_candidates(project, job, "review") == [expected]
    assert models.control_candidates(project, job, "coordinator") == [expected]
    adapter = adapters.get("codex")
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"id": 1, "_model_selection": expected.to_dict()}, project.root,
                                  "reviewer", project, prompt="Review")
    assert launch.env["AGENTKIT_MODEL"] == "gpt-6.1-sol" and launch.env["AGENTKIT_MODEL_EFFORT"] == "xhigh"
    lower = {**expected.to_dict(), "effort": "high"}
    with pytest.raises(ValueError, match="default high"):
        models.for_launch(project, {"_model_selection": lower}, "codex", "reviewer")


def test_explicit_coordinator_never_substitutes_another_provider(state):
    conn, project = state
    claude_job = jobs.create(project.root, "pinned-claude", "Build it", "claude-code")
    with pytest.raises(ValueError, match="allows claude-code for that role"):
        models.control_candidates(project, claude_job, "coordinator")
    claude_reviews = jobs.create(project.root, "claude-reviews", "Build it", reviewers=["claude-code"])
    with pytest.raises(ValueError, match=r"roles\.reviewer uses codex"):
        models.control_candidates(project, claude_reviews, "review")
    auto = jobs.create(project.root, "auto-job", "Build it")
    providers.begin_cooldown(conn, "codex", reason="Codex plan allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=5))
    assert [p.provider for p in models.control_candidates(project, auto, "coordinator")] == ["codex"]
    assert [p.provider for p in models.control_candidates(project, auto, "review")] == ["codex"]


@pytest.mark.parametrize("role_name", ["coordinator", "reviewer"])
@pytest.mark.parametrize("entry,message", [
    ({**SOL, "fallback": [OPUS]}, "cannot declare a fallback"),
    ({"profile": "sonnet", "effort": "xhigh"}, "profile must be one of"),
    ({"profile": "sol", "effort": "minimal"}, "effort must be explicit"),
    ({"profile": "sol"}, "effort must be explicit"),
    ({**SOL, "temperature": 0}, "unknown keys"),
])
def test_invalid_control_entries_are_rejected(tmp_path, role_name, entry, message):
    project = ProjectConfig(root=tmp_path, raw={"model_policy": {"roles": {role_name: entry}}})
    with pytest.raises(ValueError, match=message):
        policy.role(project, role_name)
    assert policy.validate_project(project)


@pytest.mark.parametrize("legacy", ["astra", "opus"])
def test_explicit_role_fails_closed_instead_of_accepting_legacy_models(state, legacy):
    _, project = state
    selection = models.profile(project, legacy, control=True).to_dict()
    for role_name in ("reviewer", "coordinator"):
        with pytest.raises(ValueError, match="exact"):
            models.for_launch(project, {"_model_selection": selection}, selection["provider"], role_name)
    assert models.for_launch(project, {}, "codex", "reviewer").model == "gpt-6.1-sol"
    with pytest.raises(ValueError, match="no authorized model"):
        models.for_launch(project, {}, "claude-code", "reviewer")


def test_persisted_coordinator_pin_outlives_a_policy_change(tmp_path):
    legacy = ProjectConfig(root=tmp_path)
    job = jobs.create(tmp_path, "running", "Keep my coordinator", "codex")
    astra = models.control_candidates(legacy, job, "coordinator")[0]
    job = jobs.pin_coordinator(tmp_path, "running", astra)
    changed = ProjectConfig(root=tmp_path, raw={k: v for k, v in PILOT.items()})
    assert models.control_candidates(changed, job, "coordinator") == [astra]
    task = {"job_id": "running", "_model_selection": astra.to_dict()}
    assert models.for_launch(changed, task, "codex", "coordinator") == astra
    assert models.for_launch(changed, {"job_id": "running"}, "codex", "coordinator") == astra
    sol = policy.role(changed, "coordinator").primary
    with pytest.raises(ValueError, match="persisted coordinator pin"):
        models.for_launch(changed, {"job_id": "running", "_model_selection": sol.to_dict()}, "codex", "coordinator")
    with pytest.raises(ValueError, match="already pinned"):
        jobs.pin_coordinator(tmp_path, "running", sol)


def test_explicit_model_id_is_never_alias_upgraded(tmp_path):
    raw = {"model_policy": {"roles": {"reviewer": {"profile": "opus", "model": "claude-opus-5", "effort": "xhigh"}}}}
    project = ProjectConfig(root=tmp_path, raw=raw)
    assert policy.role(project, "reviewer").primary.model == "claude-opus-5"


def test_fallback_is_limited_to_approved_failure_classes(state):
    _, project = state
    backend = policy.named(project, "backend")
    assert [p.name for p in backend.candidates()] == ["opus"]
    assert [(p.name, p.effort) for p in backend.candidates("USAGE_LIMIT")] == [("opus", "xhigh"), ("sol", "xhigh")]
    for kind in ("AUTH_ERROR", "CRASH", "MODEL_UNAVAILABLE", "RATE_LIMIT", "PROVIDER_OUTAGE"):
        assert [p.name for p in backend.candidates(kind)] == ["opus"], kind
    assert [p.name for p in policy.named(project, "frontend").candidates(set(policy.TRIGGERS))] == ["sol"]


@pytest.mark.parametrize("fallback,message", [
    ([{**SOL, "when": ["CONFLICT"]}], "failure classes"),
    ([{**OPUS}], "repeats a model"),
    ([{**SOL}, {**SOL}], "repeats a model"),
    ({"profile": "sol"}, "must be a list"),
])
def test_invalid_fallback_lists_are_rejected(tmp_path, fallback, message):
    raw = {"model_policy": {"assignments": {"backend": {**OPUS, "fallback": fallback}}}}
    with pytest.raises(ValueError, match=message):
        policy.named(ProjectConfig(root=tmp_path, raw=raw), "backend")


def test_yaml_on_key_is_read_as_when(tmp_path):
    import yaml
    raw = yaml.safe_load("model_policy:\n  assignments:\n    backend:\n      profile: opus\n      effort: xhigh\n"
                         "      fallback:\n        - {profile: sol, effort: xhigh, on: [USAGE_LIMIT, CRASH]}\n")
    backend = policy.named(ProjectConfig(root=tmp_path, raw=raw), "backend")
    assert backend.fallbacks[0].when == ("USAGE_LIMIT", "CRASH")


def test_claude_usage_limit_moves_backend_to_sol_only(state):
    conn, project = state
    backend = task(conn, "backend")
    assert models.choose_worker(conn, project, backend, capable())[0].name == "opus"
    providers.begin_cooldown(conn, "claude-code", reason="Claude subscription allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=3))
    selected, _ = models.choose_worker(conn, project, backend, capable(),
                                       unavailable=scheduler.unavailable_adapters(conn))
    assert (selected.model, selected.effort) == ("gpt-6.1-sol", "xhigh")


@pytest.mark.parametrize("kind", ["AUTH_ERROR", "CRASH"])
def test_backend_never_falls_back_for_unapproved_failures(state, kind):
    conn, project = state
    backend = task(conn, "backend")
    policy.record_trigger(conn, backend["id"], kind, "claude-code", "claude-opus-5-5")
    if kind == "AUTH_ERROR":
        providers.observe(conn, "claude-code", {"available": False, "auth_error": True, "reason": "login"})
    else:
        providers.begin_cooldown(conn, "claude-code", reason="provider unavailable",
                                 retry_at=datetime.now(UTC) + timedelta(hours=1))
    selected, reason = models.choose_worker(conn, project, backend, capable(),
                                            unavailable=scheduler.unavailable_adapters(conn))
    assert selected is None and reason.startswith("provider unavailable")
    assert models.waiting_provider(conn, project, backend, scheduler.unavailable_adapters(conn)) == "claude-code"


def test_rejected_pinned_model_is_a_decision_not_a_fallback(state):
    conn, project = state
    backend = task(conn, "backend")
    models.reject(conn, "claude-code", "claude-opus-5-5", "model not found")
    policy.record_trigger(conn, backend["id"], errors.MODEL_UNAVAILABLE, "claude-code", "claude-opus-5-5")
    selected, reason = models.choose_worker(conn, project, backend, capable())
    assert selected is None and reason.startswith("no eligible model") and "approve a fallback" in reason


def test_frontend_waits_for_codex_and_never_uses_claude(state):
    conn, project = state
    frontend = task(conn, "frontend")
    providers.begin_cooldown(conn, "codex", reason="Codex plan allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=3))
    selected, reason = models.choose_worker(conn, project, frontend, capable(),
                                            unavailable=scheduler.unavailable_adapters(conn))
    assert selected is None and reason.startswith("provider unavailable")
    assert "opus" not in reason and "claude" not in reason


def test_worker_usage_limit_records_the_failure_class(state):
    conn, project = state
    backend = task(conn, "backend", adapter="claude-code", model="claude-opus-5-5")
    db.set_status(conn, backend["id"], "LEASED")
    db.set_status(conn, backend["id"], "RUNNING")
    quota.handle_worker_failure(conn, backend["id"], "claude-code",
                                "Claude usage limit reached. Your limit will reset at 3pm.", 1)
    assert policy.trigger(conn, backend["id"]) == errors.USAGE_LIMIT
    assert db.get_task(conn, backend["id"])["attempts"] == 0


def test_scheduler_plans_the_approved_fallback(state):
    conn, project = state
    save_cache(project.root, capable())
    backend = task(conn, "backend")
    providers.begin_cooldown(conn, "claude-code", reason="Claude subscription allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=3))
    plans, report = scheduler.plan(conn, project.root, project)
    assert [(p.task["id"], p.adapter_name, p.model_selection["model"]) for p in plans] == [
        (backend["id"], "codex", "gpt-6.1-sol")]


def test_task_definition_validates_assignments(project_root):
    with pytest.raises(ValueError, match="not both"):
        spec.TaskSpec.from_dict({"id": "x", "title": "X", "model_profile": "sol", "model_assignment": "backend"})
    with pytest.raises(ValueError, match="invalid model_assignment"):
        spec.TaskSpec.from_dict({"id": "x", "title": "X", "model_assignment": "Bad Name"})
    with pytest.raises(ValueError, match="unknown model assignment"):
        planning.create(project_root, title="Unknown pin", expected_write=["services/retry.py"],
                        model_assignment="backend")
    identifier = planning.create(project_root, title="Inline pin", expected_write=["services/retry.py"],
                                 model_assignment={**SOL})
    conn = db.connect(project_root)
    try:
        stored = db.get_task(conn, identifier)
        assert policy.for_task(ProjectConfig(root=project_root), stored).primary.model == "gpt-6.1-sol"
    finally:
        conn.close()
    restored = spec.load(project_root)[-1]
    assert restored.model_assignment == SOL


def test_small_models_cannot_be_pinned_to_hard_work(tmp_path):
    raw = {"model_policy": {"assignments": {"cheap": {"profile": "sonnet", "effort": "high"}}}}
    project = ProjectConfig(root=tmp_path, raw=raw)
    with pytest.raises(ValueError, match="easy task"):
        policy.for_task(project, {"model_assignment": "cheap", "kind": "SAFE_PARALLEL", "complexity": "complex"})


def test_reviewer_cannot_approve_from_the_worker_session(state):
    conn, project = state
    work = task(conn, "frontend")
    db.update_task(conn, work["id"], session_token="worker-session")
    worker = conn.execute("INSERT INTO processes(purpose,provider,task_id,session_token,launch_json,started_at,status) "
                          "VALUES('worker','codex',?,'worker-session','{}',?,'FINISHED')", (work["id"], db.utcnow())).lastrowid
    reviewer = conn.execute("INSERT INTO processes(purpose,provider,task_id,session_token,launch_json,started_at) "
                            "VALUES('review','codex',?,'worker-session','{}',?)", (work["id"], db.utcnow())).lastrowid
    current = db.get_task(conn, work["id"])
    with pytest.raises(PermissionError, match="fresh independent session"):
        reviews.ensure_independent(conn, current, processes.get(conn, reviewer))
    with pytest.raises(PermissionError, match="own commit"):
        reviews.ensure_independent(conn, current, processes.get(conn, worker))
    processes.update(conn, reviewer, session_token="fresh-review-session")
    reviews.ensure_independent(conn, current, processes.get(conn, reviewer))


def test_expired_quota_window_does_not_authorize_outage_fallback(state):
    conn, project = state
    backend = task(conn, "backend")
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)", ("claude-code:default", "claude", "five_hour", 100, old, old, "old event"))
    providers.begin_cooldown(conn, "claude-code", reason="provider unreachable")
    assert policy.provider_trigger(conn, "claude-code") == "PROVIDER_OUTAGE"
    assert models.choose_worker(conn, project, backend, capable())[0] is None


def test_saved_coordinator_pin_survives_disabled_profile_and_invalid_current_role(state):
    conn, project = state
    job = jobs.create(project.root, "saved-manager", "Build", "codex")
    pinned = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)
    jobs.pin_coordinator(project.root, job["id"], pinned)
    project.raw = {"model_policy": {"profiles": {"sol": {"enabled": False}}, "roles": {"coordinator": "invalid"}}}
    assert models.for_launch(project, {"job_id": job["id"]}, "codex", "coordinator") == pinned
    assert models.usable(conn, project, pinned, pinned=True)
    assert models.control_candidates(project, jobs.load(project.root, job["id"]), "coordinator") == [pinned]


@pytest.mark.parametrize("effort", ["medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("profile_name", ["sol", "opus"])
def test_explicit_control_effort_is_configurable_but_existing_pin_is_sticky(tmp_path, effort, profile_name):
    entry = {"profile": profile_name, "effort": effort}
    project = ProjectConfig(root=tmp_path, raw={"model_policy": {"roles": {"coordinator": entry}}})
    chosen = policy.role(project, "coordinator").primary
    assert chosen.effort == effort and policy.control_allowed(project, "coordinator", chosen)
    pin = {**chosen.to_dict(), "effort": "xhigh"}
    assert policy.control_allowed(project, "coordinator", models.Profile(**pin), pin)
    assert policy.control_allowed(project, "coordinator", chosen, pin) == (effort == "xhigh")
