"""One agent per feature: a builder plans, implements and tests its feature in one session.

The separate builder/tester split re-read the same spec and code in a cold session
per role and chained them serially; on the benchmark no tasks could run in parallel
and nobody owned the whole behavior. `workflow.mode: feature` is the default for new
projects: one builder owns a feature end to end, a retry resumes its session, and an
independent reviewer checks the final commit. `separate-tasks` remains available.
"""
from __future__ import annotations

import pytest
import yaml

from agentkit import review_policy, workflow
from agentkit.config import ProjectConfig, load_project
from agentkit.run_limits import limits


def config(root, mode="feature", **settings):
    return ProjectConfig(root=root, raw={"workflow": {"mode": mode, **settings}},
                         gates={"feature": ["python -m pytest tests/test_unit.py -q"]})


def assignment(**changes):
    return {"id": "unit", "title": "Bounded retry", "description": "Implement and test bounded retry.",
            "role": "backend-builder", "gate_level": "feature",
            "expected_paths": {"write": ["src/retry.py", "tests/test_retry.py"], "read": []}, **changes}


def test_a_feature_builder_writes_source_and_tests_and_runs_them(tmp_path):
    workflow.validate_task(config(tmp_path), assignment())
    workflow.validate_gate(config(tmp_path), assignment(), "feature")
    with pytest.raises(ValueError, match="builder may not write test files"):
        workflow.validate_task(config(tmp_path, "separate-tasks"), assignment())


def test_the_planner_default_role_is_a_feature_builder(tmp_path):
    workflow.validate_task(config(tmp_path), assignment(role="implementer"))
    text = workflow.role_text(config(tmp_path), "implementer")
    assert "end to end" in text and "tests" in text


@pytest.mark.parametrize("role", ["backend-builder", "frontend-builder"])
def test_feature_builders_own_the_whole_feature(tmp_path, role):
    text = workflow.role_text(config(tmp_path), role)
    assert "end to end" in text and "independent reviewer" in text
    assert "Do not write, edit or run tests" not in text
    assert "Do not write, edit or run tests" in workflow.role_text(config(tmp_path, "separate-tasks"), role)


def test_a_retry_resumes_the_feature_session(tmp_path):
    task = {"session_token": "builder-session"}
    assert workflow.resume_token(config(tmp_path), task, True) == "builder-session"
    assert workflow.resume_token(config(tmp_path), task, False) is None
    assert workflow.resume_token(config(tmp_path, "separate-tasks"), task, True) is None
    assert workflow.resume_token(ProjectConfig(root=tmp_path), task, True) == "builder-session"


def test_feature_limits_fit_a_feature(tmp_path):
    feature, separate = config(tmp_path), config(tmp_path, "separate-tasks")
    many = assignment(expected_paths={"write": [f"src/part_{i}.py" for i in range(10)], "read": []})
    workflow.validate_task(feature, many)
    with pytest.raises(ValueError, match="max_write_files"):
        workflow.validate_task(separate, many)
    assert limits(feature)["max_turns"] > limits(separate)["max_turns"]
    assert limits(feature)["max_runtime_seconds"] > limits(separate)["max_runtime_seconds"]


def test_both_managed_modes_share_the_managed_machinery(tmp_path):
    assert workflow.enabled(config(tmp_path)) and workflow.feature(config(tmp_path))
    assert workflow.enabled(config(tmp_path, "separate-tasks")) and not workflow.feature(config(tmp_path, "separate-tasks"))
    assert not workflow.enabled(ProjectConfig(root=tmp_path))
    assert review_policy.mode(config(tmp_path, review="human")) == "human"


def test_new_projects_start_in_feature_mode(repo):
    from agentkit.init_project import init
    init(repo)
    raw = yaml.safe_load((repo / ".ai" / "project.yaml").read_text(encoding="utf-8"))
    assert raw["workflow"]["mode"] == "feature"
    assert workflow.feature(load_project(repo))


def test_the_manager_is_told_to_plan_whole_features(tmp_path):
    note = workflow.planning_note(config(tmp_path))
    assert "one task per feature" in note and "do not create separate tester tasks" in note
    assert workflow.planning_note(config(tmp_path, "separate-tasks")) == ""


def test_settings_switch_the_workflow_while_stopped(project_root):
    from agentkit import repo_settings
    assert repo_settings.read(project_root)["mode"] == "legacy"
    repo_settings.apply(project_root, {"mode": "feature"})
    assert repo_settings.read(project_root)["mode"] == "feature"
    repo_settings.apply(project_root, {"mode": "separate-tasks"})
    assert workflow.enabled(load_project(project_root)) and not workflow.feature(load_project(project_root))
    with pytest.raises(ValueError):
        repo_settings.apply(project_root, {"mode": "swarm"})
