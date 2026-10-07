"""Operator setup commands propagate failures; default profiles reuse components."""
from types import SimpleNamespace

import yaml

from agentkit import cli_workspaces, db, environment_defaults, gates


def test_setup_prepare_failure_has_nonzero_exit(conn, project, monkeypatch, capsys):
    task_id = db.create_task(conn, title="setup", worktree=str(project.root))
    monkeypatch.setattr("agentkit.environment_prepare.prepare",
                        lambda *a: gates.GateResult("worktree_setup", False, skipped_reason="blocked"))
    args = SimpleNamespace(path=project.root, workspace_action="setup-prepare", task=task_id)
    assert cli_workspaces.command(args) == 1
    assert '"passed": false' in capsys.readouterr().out


def test_init_combined_profile_uses_components_without_duplicate_commands():
    found = SimpleNamespace(setup=["python -m venv venv", "npm ci"],
                            stacks=["python", "javascript"], gates={"full": ["python -m pytest", "npm test"]})
    config = yaml.safe_load("\n".join(environment_defaults.render(found)))
    combined = config["environment_profiles"]["combined"]
    assert combined["setup"] == []
    assert combined["components"] == ["python", "javascript"]
    assert config["environment_gates"]["full"] == "combined"
