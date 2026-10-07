"""Omitted gate levels honor assignment without weakening explicit restrictions."""
import pytest
import yaml

from agentkit import db, gates, mcp_server


def setup_task(conn, project_root, monkeypatch):
    config = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["workflow"] = {"mode": "separate-tasks"}
    raw["gates"]["source-check"] = raw["gates"]["fast"]
    config.write_text(yaml.safe_dump(raw))
    task_id = db.create_task(conn, title="Source", status="RUNNING", generation=1,
                             role="backend-builder", gate_level="source-check",
                             worktree=str(project_root))
    for key, value in {"AGENTKIT_ROOT": str(project_root), "AGENTKIT_TASK": str(task_id),
                       "AGENTKIT_GENERATION": "1", "AGENTKIT_ROLE": "worker"}.items():
        monkeypatch.setenv(key, value)
    return task_id


def test_omitted_level_uses_assigned_gate(conn, project_root, monkeypatch):
    task_id = setup_task(conn, project_root, monkeypatch)
    seen = []
    def run(project, level, **kwargs):
        seen.append(level)
        return gates.GateResult(level, True)
    monkeypatch.setattr(mcp_server.gates, "run_gate", run)
    mcp_server.gate_run()
    assert seen == ["source-check"]
    event = conn.execute("SELECT level FROM gate_results WHERE task_id=?",
                         (task_id,)).fetchone()
    assert event[0] == "source-check"


def test_explicit_wrong_gate_is_refused_before_execution(conn, project_root, monkeypatch):
    setup_task(conn, project_root, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("wrong gate must never execute")
    monkeypatch.setattr(mcp_server.gates, "run_gate", forbidden)
    with pytest.raises(ValueError):
        mcp_server.gate_run(level="fast")


def test_omitted_level_outside_task_preserves_fast(project_root, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    monkeypatch.chdir(project_root)
    seen = []
    def run(project, level, **kwargs):
        seen.append(level)
        return gates.GateResult(level, True)
    monkeypatch.setattr(mcp_server.gates, "run_gate", run)
    mcp_server.gate_run()
    assert seen == ["fast"]
