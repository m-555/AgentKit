"""Model policy, fallback and local-worker boundaries; no paid inference."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    adapters,
    briefs,
    db,
    errors,
    jobs,
    models,
    processes,
    providers,
    runner,
    scheduler,
    spec,
    worktrees,
)
from agentkit.adapters.local_opencode import LocalOpenCodeAdapter, local_config
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import ProjectConfig


@pytest.fixture
def state(tmp_path):
    conn = db.connect(tmp_path)
    yield conn, ProjectConfig(root=tmp_path)
    conn.close()


def capable():
    result = {}
    for provider in ("codex", "claude-code", "local-opencode"):
        caps = CapabilitySet(adapter=provider)
        for key in caps.values:
            caps.set(key, True)
        result[provider] = caps
    return result


def choose(state, **fields):
    conn, project = state
    return models.choose_worker(conn, project, {"kind": "SAFE_PARALLEL", **fields}, capable())[0]


def test_worker_preference_and_provider_quota(state):
    conn, project = state
    assert choose(state).name == "sol"
    assert choose(state, model_profile="opus").name == "opus"
    providers.begin_cooldown(conn, "codex", reason="5h quota", retry_at=datetime.now(UTC) + timedelta(hours=1))
    assert choose(state).name == "opus"
    providers.begin_cooldown(conn, "claude-code", reason="weekly quota", retry_at=datetime.now(UTC) + timedelta(days=1))
    assert choose(state, complexity="complex") is None


@pytest.mark.parametrize("profile", ["sonnet", "qwen"])
@pytest.mark.parametrize("kind,complexity", [("SAFE_PARALLEL", "complex"), ("HOTSPOT", "easy"), ("CONTRACT_CHANGE", "easy")])
def test_small_models_cannot_receive_difficult_work(profile, kind, complexity):
    with pytest.raises(ValueError):
        spec.TaskSpec.from_dict({"id": "test", "title": "Test", "kind": kind,
                                "complexity": complexity, "model_profile": profile})


def test_local_assignment_is_explicit_readonly_and_promotes_after_failure(state):
    assert choose(state, kind="RESEARCH", complexity="easy").name == "sol"
    assert choose(state, kind="RESEARCH", complexity="easy", model_profile="qwen").name == "qwen"
    assert choose(state, complexity="easy", model_profile="qwen").name == "sol"
    assert choose(state, kind="RESEARCH", complexity="easy", model_profile="qwen", attempts=1).name == "sol"


def test_unavailable_model_does_not_cool_other_models_on_account(state):
    conn, project = state
    models.reject(conn, "codex", "gpt-6-astra", "model not found")
    assert providers.is_available(conn, "codex")
    assert choose(state).name == "sol"
    assert not models.usable(conn, project, models.profile(project, "astra"))


def test_control_fallback_and_pinning_preserve_memory(state):
    conn, project = state
    job = jobs.create(project.root, "draft", "Build an offline calendar")
    candidates = models.control_candidates(project, job, "coordinator")
    assert [(p.name, p.effort) for p in candidates] == [("astra", "high"), ("opus", "high")]
    pinned = jobs.pin_coordinator(project.root, "draft", candidates[0])
    providers.begin_cooldown(conn, "codex", reason="limit", retry_at=datetime.now(UTC) + timedelta(hours=1))
    assert models.control_candidates(project, pinned, "coordinator") == [candidates[0]]
    assert not models.usable(conn, project, candidates[0])
    assert models.usable(conn, project, models.control_candidates(project, pinned, "review")[1])
    project.raw = {"model_policy": {"profiles": {"astra": {"model": "different-model"}}}}
    assert models.control_candidates(project, pinned, "coordinator")[0].model == "gpt-6-astra"
    with pytest.raises(ValueError, match="pinned"):
        jobs.pin_coordinator(project.root, "draft", candidates[1])
    assert jobs.load(project.root, "draft")["requests"][0]["text"] == "Build an offline calendar"


@pytest.mark.parametrize("started", [False, True])
def test_rejected_initial_model_can_fallback_only_before_work(state, started):
    conn, project = state
    jobs.create(project.root, "draft", "Keep my requirements")
    jobs.pin_coordinator(project.root, "draft", models.profile(project, "astra", control=True))
    if started:
        jobs.amend(project.root, "draft", "Use existing data store")
    jobs.initial_model_rejected(project.root, conn, "draft")
    job = jobs.load(project.root, "draft")
    assert job["coordinator"] == ("codex" if started else "auto")
    assert bool(job.get("coordinator_model")) == started
    assert job["requests"][0]["text"] == "Keep my requirements"


@pytest.mark.parametrize("provider,role,model,effort", [
    ("codex", "implementer", "gpt-6.1-sol", "high"),
    ("codex", "coordinator", "gpt-6-astra", "high"),
    ("codex", "reviewer", "gpt-6-astra", "high"),
    ("claude-code", "implementer", "claude-opus-5-5", "high"),
    ("claude-code", "coordinator", "claude-opus-5-5", "high"),
    ("claude-code", "reviewer", "claude-opus-5-5", "high"),
])
def test_actual_launch_uses_required_model_and_effort(state, monkeypatch, provider, role, model, effort):
    _, project = state
    adapter = adapters.get(provider)
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"id": 1}, project.root, role, project, prompt="Test")
    assert launch.env["AGENTKIT_MODEL"] == model
    assert launch.env["AGENTKIT_MODEL_EFFORT"] == effort
    assert model in launch.argv
    if provider == "claude-code":
        assert launch.argv[launch.argv.index("--effort") + 1] == effort
    else:
        assert f'model_reasoning_effort="{effort}"' in launch.argv


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    root = tmp_path / "local-opencode"
    root.mkdir()
    data = {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:8080/v1"},
            "models": {"qwen3.8-27b-q8-tuber": {"limit": {"context": 32768, "output": 8192}}, "other-model": {}}}},
            "agent": {"orchestrator": {"permission": "allow"}}, "mcp": {"unrelated": {"command": ["bad"]}}}
    path = root / "opencode.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENTKIT_LOCAL_OPENCODE", str(root))
    return path


def test_local_config_does_not_import_other_workflow(state, local_source, monkeypatch):
    _, project = state
    adapter = LocalOpenCodeAdapter()
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"id": 1, "kind": "RESEARCH", "complexity": "easy", "model_profile": "qwen"},
                                  project.root, "researcher", project, prompt="Locate entry points")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert list(config["agent"]) == ["agentkit-worker"]
    assert list(config["mcp"]) == ["agentkit"]
    assert list(config["provider"]["local"]["models"]) == ["qwen3.8-27b-q8-tuber"]
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["agentkit_checkpoint"] == "allow"
    assert "--pure" in launch.argv and "--auto" not in launch.argv
    assert launch.stdin_text == "Locate entry points"
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert str(project.root / ".ai/runtime/local-opencode") in launch.env[key]
    assert "orchestrator" in json.loads(local_source.read_text())["agent"]
    assert not adapter.default_capabilities().can_run("SAFE_PARALLEL")
    with pytest.raises(ValueError, match="RESEARCH"):
        adapter.build_launch({"id": 1, "kind": "SAFE_PARALLEL"}, project.root, "implementer", project, prompt="Edit")


@pytest.mark.parametrize("url", ["http://0.0.0.0:8080/v1", "https://example.com/v1", "http://127.0.0.1:8080/v1?token=foo"])
def test_local_config_refuses_nonloopback_endpoints(local_source, url):
    data = json.loads(local_source.read_text())
    data["provider"]["local"]["options"]["baseURL"] = url
    local_source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        local_config()


def test_local_events_keep_session_and_answer():
    events = [{"type": "step_start", "sessionID": "ses_123"},
              {"type": "text", "sessionID": "ses_123", "part": {"text": "Entry point is main.py"}},
              {"type": "error", "error": {"message": "model not found"}}]
    parsed = list(LocalOpenCodeAdapter().parse_events(io.StringIO("\n".join(map(json.dumps, events)))))
    assert parsed[0].detail["session_id"] == "ses_123"
    assert parsed[1].detail["text"] == "Entry point is main.py"
    assert parsed[-1].detail["is_error"]


def test_gpu_slot_is_reserved_in_plan_and_enforced_in_db(state):
    conn, project = state
    save_cache(project.root, capable())
    for title in ("first", "second"):
        db.create_task(conn, title=title, kind="RESEARCH", complexity="easy", model_profile="qwen", status="READY")
    plans, _ = scheduler.plan(conn, project.root, project)
    assert [p.model_selection["name"] for p in plans] == ["qwen", "sol"]
    conn.execute("INSERT INTO processes(purpose,provider,launch_json,started_at) VALUES('worker','local-opencode','{}',?)", (db.utcnow(),))
    assert choose(state, kind="RESEARCH", complexity="easy", model_profile="qwen").name == "sol"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO processes(purpose,provider,launch_json,started_at) VALUES('worker','local-opencode','{}',?)", (db.utcnow(),))


def test_model_rejection_requeues_without_charging_task_or_account(state):
    conn, project = state
    task_id = db.create_task(conn, title="Worker", status="RUNNING", adapter="codex", generation=1)
    launch = {"argv": [sys.executable, "-c", "import sys; sys.stderr.write('model gpt-6.1-sol not found'); sys.exit(1)"],
              "cwd": str(project.root), "env": {"AGENTKIT_MODEL": "gpt-6.1-sol"}, "stdin_text": None}
    cursor = conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at) VALUES('worker','codex',?,1,?,?)",
                          (task_id, json.dumps(launch), db.utcnow()))
    runner.run(project.root, cursor.lastrowid)
    task = db.get_task(conn, task_id)
    assert task["status"] == "READY" and task["attempts"] == 0
    assert providers.is_available(conn, "codex")
    assert not models.usable(conn, project, models.profile(project, "sol"))
    assert processes.get(conn, cursor.lastrowid)["status"] == "FAILED"


def test_quota_and_model_rejection_are_distinct():
    assert errors.classify("model gpt-6-astra does not exist", 1).kind == errors.MODEL_UNAVAILABLE
    assert errors.classify("weekly limit reached", 1).kind == errors.USAGE_LIMIT


def test_model_selection_survives_task_spec_roundtrip():
    task = spec.TaskSpec.from_dict({"id": "locate", "title": "Locate entry points", "kind": "RESEARCH",
                                   "complexity": "easy", "model_profile": "qwen"})
    restored = spec.TaskSpec.from_dict(task.to_dict())
    assert restored == task
    restored.model_profile = "sol"
    assert restored.hash() != task.hash()


@pytest.mark.parametrize("role", ["coordinator", "reviewer"])
def test_control_role_cannot_be_assigned_a_worker_profile(state, role):
    _, project = state
    with pytest.raises(ValueError, match="supervisor-controlled"):
        spec.TaskSpec.from_dict({"id": "test", "title": "Test", "role": role})
    with pytest.raises(ValueError, match="default high"):
        models.for_launch(project, {"_model_selection": models.profile(project, "sol").to_dict()}, "codex", role)


def test_research_findings_survive_mechanical_checkpoint(state):
    conn, project = state
    task_id = db.create_task(conn, title="Map module", kind="RESEARCH")
    db.write_checkpoint(conn, task_id, {"completed": ["Entry point: main.py"], "decisions": ["Reuse the existing parser"]}, kind="semantic", head_sha="abc123")
    db.write_checkpoint(conn, task_id, {"head_sha": "abc123", "dirty_files": []}, kind="mechanical")
    packet = briefs.build(conn, project, task_id)
    assert packet["last_checkpoint"]["head_sha"] == "abc123"
    rendered = briefs.render(packet)
    assert "Entry point: main.py" in rendered and "Reuse the existing parser" in rendered


@pytest.mark.parametrize("previous_model,resumed", [("claude-opus-5-5", True), ("claude-opus-5", False), ("claude-sonnet-5", False)])
def test_scheduler_resumes_only_the_same_model(project_root, conn, project, monkeypatch, previous_model, resumed):
    task_id = db.create_task(conn, title="Resume worker", spec_id="resume-worker", status="READY",
                             adapter="claude-code", model=previous_model,
                             expected_write=["services/retry.py"])
    db.update_task(conn, task_id, session_token="old-session")
    task = db.get_task(conn, task_id)
    work, _ = worktrees.ensure(project_root, task, project)
    save_cache(project_root, capable())
    recorded = []
    def start(connection, root, launch, **fields):
        recorded.append(launch)
        return connection.execute("INSERT INTO processes(purpose,provider,task_id,launch_json,started_at) VALUES('worker','claude-code',?,'{}',?)",
                                  (task_id, db.utcnow())).lastrowid
    monkeypatch.setattr(processes, "start", start)
    monkeypatch.setattr(adapters.get("claude-code"), "detect", lambda: None)
    plan = scheduler.LaunchPlan(task, "claude-code", work, 1, "test resume", models.profile(project, "opus").to_dict())
    ok, detail = scheduler.launch(conn, project_root, project, plan)
    assert ok, detail
    assert ("--resume" in recorded[0].argv) == resumed
    assert db.get_task(conn, task_id)["model"] == "claude-opus-5-5"
    assert db.get_task(conn, task_id)["session_token"] == ("old-session" if resumed else None)


def test_cli_exposes_model_policy(state, capsys):
    from agentkit.cli import main
    _, project = state
    (project.root / ".git").mkdir()
    assert main(["--path", str(project.root), "models", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in entries] == ["astra", "sol", "opus", "sonnet", "qwen"]


@pytest.mark.parametrize("name,old,new", [
    ("sol", "gpt-5.6-sol", "gpt-6.1-sol"),
    ("sol", "gpt-6-sol", "gpt-6.1-sol"),
    ("opus", "claude-opus-5", "claude-opus-5-5"),
])
def test_approved_upgrade_of_existing_project_defaults_preserves_pins(state, name, old, new):
    _, project = state
    settings = {"model": old}
    project.raw = {"model_policy": {"profiles": {name: settings}}}
    assert models.profile(project, name).model == new
    settings["pinned"] = True
    assert models.profile(project, name).model == old
    settings.update(model="custom-snapshot", pinned=False)
    assert models.profile(project, name).model == "custom-snapshot"


def test_opus_upgrade_never_changes_an_existing_coordinator_session(state):
    _, project = state
    jobs.create(project.root, "existing", "Keep my original requirements")
    old = models.Profile("opus", "claude-code", "claude-opus-5", "xhigh", 2)
    job = jobs.pin_coordinator(project.root, "existing", old)
    assert models.control_candidates(project, job, "coordinator") == [old]
    assert models.control_candidates(project, job, "review")[1].model == "claude-opus-5-5"


@pytest.mark.parametrize("effort", ["none", "minimal", None])
def test_sol_upgrade_rejects_unsupported_effort(state, effort):
    _, project = state
    project.raw = {"model_policy": {"profiles": {"sol": {"model": "gpt-5.6-sol", "effort": effort}}}}
    with pytest.raises(ValueError, match="unsupported"):
        models.profile(project, "sol")
