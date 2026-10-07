"""Separate task policy is enforced before writes and across fresh launches."""

import json

import pytest

from agentkit import briefs, db, instructions, jobs, planning, spec, workflow
from agentkit.config import ProjectConfig
from agentkit.run_limits import limits


def config(root, **settings):
    return ProjectConfig(
        root=root,
        raw={"workflow": {"mode": "separate-tasks", **settings}},
        gates={
            "source": ["python -m ruff check src/unit.py"],
            "tests": ["python -m pytest tests/test_unit.py"],
        },
    )


def task(**changes):
    return {
        "id": "unit",
        "title": "One change",
        "description": "Implement one function.",
        "role": "backend-builder",
        "expected_paths": {"write": ["src/unit.py"], "read": []},
        "gate_level": "source",
        **changes,
    }


def test_task_spec_and_runtime_paths_are_both_valid(tmp_path):
    project = config(tmp_path)
    definition = spec.TaskSpec(
        spec_id="unit",
        title="One change",
        role="backend-builder",
        expected_write=["src/unit.py"],
        gate_level="source",
    )
    workflow.validate_task(project, definition)
    workflow.validate_task(project, definition.to_dict())
    workflow.validate_task(project, task(expected_paths=None, expected_write='["src/unit.py"]'))


@pytest.mark.parametrize(
    "change",
    [
        {"description": "x" * 1201},
        {"role": "implementer"},
        {"expected_paths": {"write": ["src/*.py"]}},
        {"expected_paths": {"write": ["tests/test_unit.py"]}},
        {"expected_paths": {"write": [1]}},
        {"kind": "TEST_ONLY"},
        {"expected_paths": {"write": [f"src/{i}.py" for i in range(4)]}},
    ],
)
def test_invalid_task_is_refused(tmp_path, change):
    with pytest.raises(ValueError):
        workflow.validate_task(config(tmp_path), task(**change))


def test_tester_depends_on_implementation_and_cannot_write_source(tmp_path):
    project = config(tmp_path)
    good = task(
        role="backend-tester",
        kind="TEST_ONLY",
        gate_level="tests",
        depends_on=["implementation"],
        expected_paths={"write": ["tests/test_unit.py"]},
    )
    workflow.validate_task(project, good)
    for changes in ({"depends_on": []}, {"expected_paths": {"write": ["src/unit.py"]}}):
        with pytest.raises(ValueError):
            workflow.validate_task(project, {**good, **changes})


def test_gate_separation_is_enforced(tmp_path):
    project = config(tmp_path)
    with pytest.raises(ValueError, match="assigned gate"):
        workflow.validate_gate(project, task(), "tests")
    project.gates["source"] = ["python -m pytest -q"]
    with pytest.raises(ValueError, match="source-only"):
        workflow.validate_task(project, task())


def test_replanning_preserves_all_user_constraints_without_operational_history(project_root):
    jobs.create(
        project_root, "compact", "Preserve existing behavior", acceptance=["Works offline"]
    )
    jobs.amend(project_root, "compact", "No Qwen for this project", user=True)
    jobs.amend(project_root, "compact", "large operational history " * 2000)
    project = config(project_root)
    memory = json.loads(workflow.job_context(project, "compact", {"job_id": "compact"}))
    assert [request["text"] for request in memory["requests"]] == [
        "Preserve existing behavior",
        "No Qwen for this project",
    ]
    assert memory["acceptance"] == ["Works offline"] and "decisions" not in memory
    jobs.amend(project_root, "compact", "x" * 16001, user=True)
    with pytest.raises(ValueError, match="replan"):
        workflow.job_context(project, "compact", {})


def test_fresh_session_rule_retains_legacy_compatibility(tmp_path):
    old = {"session_token": "old-provider-session"}
    assert workflow.resume_token(config(tmp_path), old, True) is None
    assert (
        workflow.resume_token(ProjectConfig(root=tmp_path), old, True) == old["session_token"]
    )
    assert workflow.resume_token(ProjectConfig(root=tmp_path), old, False) is None
    assert limits(config(tmp_path))["max_turns"] == 16


def test_invalid_definition_does_not_touch_graph_or_database(project_root):
    path = project_root / ".ai/project.yaml"
    path.write_text("workflow: {mode: separate-tasks}\ngates: {source: ['echo source']}\n")
    before = (project_root / ".ai/tasks.yaml").read_bytes()
    connection = db.connect(project_root)
    state = list(connection.iterdump())
    definition = spec.TaskSpec(
        spec_id="invalid",
        title="Bad",
        role="backend-builder",
        expected_write=["tests/test_bad.py"],
        gate_level="source",
    )
    with pytest.raises(ValueError):
        planning.put(project_root, definition)
    assert (project_root / ".ai/tasks.yaml").read_bytes() == before
    assert list(connection.iterdump()) == state
    connection.close()


def test_brief_has_bounded_intent_but_launch_does_not_duplicate_it(project_root, conn):
    jobs.create(project_root, "small", "Preserve behavior")
    project = config(project_root)
    identifier = db.create_task(
        conn,
        title="One",
        role="backend-builder",
        job_id="small",
        owned_paths=["src/unit.py"],
        gate_level="source",
        status="RUNNING",
    )
    row = db.get_task(conn, identifier)
    launch = instructions.prompt(row, project, "Call brief once. Do not delegate.")
    assert "Preserve behavior" not in launch and "Do not write, edit or run tests" in launch
    assert "Preserve behavior" in briefs.render(briefs.build(conn, project, identifier))
    db.write_checkpoint(conn, identifier, {"completed": ["x" * 20000]}, kind="semantic")
    with pytest.raises(ValueError, match="context"):
        briefs.build(conn, project, identifier)


@pytest.mark.parametrize("path", ["../escape.py", "C:/escape.py", "/escape.py", "src/../../escape.py"])
def test_exact_scope_refuses_escaping_paths(tmp_path, path):
    with pytest.raises(ValueError, match="escaping"):
        workflow.validate_task(config(tmp_path), task(expected_paths={"write": [path], "read": []}))


@pytest.mark.parametrize("provider", ["claude-code", "codex", "local-opencode"])
def test_fresh_launch_flags_for_each_provider_without_running_models(tmp_path, monkeypatch, provider):
    from agentkit import adapters
    from agentkit.adapters import local_opencode
    project = config(tmp_path)
    project.budgets = {}
    worker = adapters.get(provider)
    monkeypatch.setattr(worker, "detect", lambda: None)
    assignment = {"id": 1, "kind": "SAFE_PARALLEL", "model_profile": "opus" if provider == "claude-code" else "sol"}
    if provider == "local-opencode":
        assignment.update(kind="RESEARCH", complexity="easy", model_profile="qwen")
        monkeypatch.setattr(local_opencode, "local_config", lambda: {
            "options": {"baseURL": "http://127.0.0.1:8080/v1"},
            "models": {"qwen3.8-27b-q8-tuber": {}}})
    launch = worker.build_launch(assignment, tmp_path, "backend-builder", project,
                                 prompt="One task", resume_token="old-session")
    assert "old-session" not in launch.argv
    if provider == "claude-code":
        assert launch.argv[launch.argv.index("--max-turns") + 1] == "16"
        assert "Task" not in launch.argv[launch.argv.index("--tools") + 1]
    elif provider == "codex":
        assert "features.multi_agent=false" in launch.argv and "features.multi_agent_v2=false" in launch.argv


def test_launch_combines_brief_and_initial_prompt_before_any_model_request(project_root, conn):
    project = config(project_root)
    identifier = db.create_task(conn, title="Compact", role="backend-builder", gate_level="source",
                                owned_paths=["services/retry.py"], status="READY")
    row = db.get_task(conn, identifier)
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="assembled"):
        workflow.validate_launch_context(conn, project, row, "x" * 15500, row["owned_paths"], project_root)
    assert list(conn.iterdump()) == before


def test_manager_can_plan_large_intent_without_duplicating_it_in_launch(project_root):
    jobs.create(project_root, "large", "x" * 20000)
    project = config(project_root)
    row = {"job_id": "large", "role": "coordinator"}
    prompt = instructions.prompt(row, project, "Call job_brief once.", role="coordinator")
    assert "Call job_brief once." in prompt and "x" * 20000 not in prompt
    with pytest.raises(ValueError, match="replan"):
        instructions.prompt({**row, "role": "backend-builder"}, project, "Call brief.")
