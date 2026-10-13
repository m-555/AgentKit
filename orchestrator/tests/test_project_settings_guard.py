"""Project settings belong to the user; a commit does not need a `fast` gate.

In the Sonnet benchmark (2026-10-12) the assigner called project_configure in the
middle of a job. It removed the `fast` gate, added a hot path, and replaced the
contract path `SPEC.md` with a sentence. Every builder commit then failed, because
the host commit always ran the `fast` gate. Under a workflow the assigner plans
within the user's settings (the view's Settings tab, or project.yaml). A project
without a `fast` gate commits normally, and the task's own gate still runs after
the commit.
"""
from __future__ import annotations

import pytest
import yaml

from agentkit import mcp_server, mcp_workspaces
from agentkit.config import load_project
from tests.test_host_commit import running


def configure(project_root, **raw_changes):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.update(raw_changes)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


@pytest.fixture
def planner(monkeypatch, project_root):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setattr(mcp_server, "_require_planner", lambda job_id=None: None)


def test_the_assigner_cannot_rewrite_configured_settings_under_a_workflow(project_root, planner):
    configure(project_root, workflow={"mode": "feature", "review": "once"})
    before = (project_root / ".ai/project.yaml").read_text(encoding="utf-8")
    with pytest.raises(PermissionError, match="belong to the user"):
        mcp_server.project_configure({"full": ["python -m unittest"]}, [], ["x.py"], ["a sentence"])
    assert (project_root / ".ai/project.yaml").read_text(encoding="utf-8") == before


def test_without_a_workflow_the_coordinator_still_configures_the_project(project_root, planner):
    mcp_server.project_configure({"full": ["python -m unittest"]}, [], [], ["contracts/**"])
    assert load_project(project_root).gate("full") == ["python -m unittest"]


def test_a_project_without_a_fast_gate_still_commits(conn, project_root, project, monkeypatch):
    project = configure(project_root, gates={"full": ['python -c "print(1)"']})
    task_id, work = running(conn, project_root, project, monkeypatch)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    assert mcp_workspaces.task_commit("Implement retry").startswith("Committed ")


def test_a_failing_fast_gate_still_blocks_the_commit(conn, project_root, project, monkeypatch):
    project = configure(project_root, gates={"fast": ['python -c "raise SystemExit(1)"'], "full": ["python -V"]})
    task_id, work = running(conn, project_root, project, monkeypatch)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    with pytest.raises(ValueError):
        mcp_workspaces.task_commit("Implement retry")
