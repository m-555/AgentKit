"""A feature builder runs its tests: a task with no named gate gets the project's full gate.

In the Sonnet benchmark (2026-10-12) the assigner named no gate, so the task got the
default `fast` gate, which only compiles. The builder was then refused the `full`
gate it asked for, and its commit was approved without its own tests having run.
The tests ran only when the work merged.
"""
from __future__ import annotations

import yaml

from agentkit import jobs, mcp_server, workflow
from agentkit.config import load_project


def configure(project_root, mode):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": mode, "review": "ai"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


def definition(**extra):
    return {"id": "bounded-retry", "job_id": "feature", "title": "Bounded retry", "role": "implementer",
            "expected_paths": {"write": ["services/retry.py", "tests/test_retry.py"]}, **extra}


def test_a_feature_builder_without_a_named_gate_gets_the_full_gate(project_root):
    project = configure(project_root, "feature")
    assert workflow.default_gate(project, definition())["gate_level"] == "full"
    assert workflow.default_gate(project, definition(gate_level="fast"))["gate_level"] == "fast"
    assert "gate_level" not in workflow.default_gate(project, definition(kind="RESEARCH", expected_paths={}))


def test_separate_tasks_keep_the_default_gate(project_root):
    project = configure(project_root, "separate-tasks")
    assert "gate_level" not in workflow.default_gate(project, definition(role="backend-builder"))


def test_the_assigner_defines_a_feature_task_with_the_full_gate(conn, project_root, monkeypatch):
    configure(project_root, "feature")
    jobs.create(project_root, "feature", "Bound the retry delay")
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setattr(mcp_server, "_require_planner", lambda job_id=None: None)
    mcp_server.task_define(definition())
    task = conn.execute("SELECT gate_level FROM tasks WHERE spec_id='bounded-retry'").fetchone()
    assert task["gate_level"] == "full"


def test_the_planning_note_asks_for_a_gate_that_runs_the_tests(project_root):
    note = workflow.planning_note(configure(project_root, "feature"))
    assert "gate that runs its tests" in note


def test_without_a_workflow_nothing_changes(project_root):
    assert "gate_level" not in workflow.default_gate(load_project(project_root), definition())
