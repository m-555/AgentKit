"""An explicit stopped-task replan clears only its own spec-change hold."""
import pytest
import yaml

from agentkit import db, planning, spec


def definition(description="first", dependencies=None):
    return spec.TaskSpec(spec_id="build", title="Build", description=description,
                         expected_write=["services/media.py"], depends_on=dependencies or [])


def test_explicit_replan_releases_only_resolved_spec_hold(conn, project_root):
    identifier = planning.put(project_root, definition())
    db.set_status(conn, identifier, "NEEDS_REPLAN", actor="scheduler")
    db.update_task(conn, identifier, blocker="spec changed while task was in flight",
                   next_action="OLD: manager must supply contract before this revision")
    planning.put(project_root, definition("accepted revision"))
    task = db.get_task(conn, identifier)
    assert task["status"] == "READY" and task["blocker"] is None
    assert task["description"] == "accepted revision"
    assert task["next_action"] is None


def test_new_dependency_keeps_replanned_task_waiting(conn, project_root):
    dependency = spec.TaskSpec(spec_id="test-input", title="Input", expected_write=["tests/input.py"])
    planning.put(project_root, dependency)
    identifier = planning.put(project_root, definition())
    db.set_status(conn, identifier, "NEEDS_REPLAN", actor="scheduler")
    db.update_task(conn, identifier, blocker="spec changed while task was in flight")
    planning.put(project_root, definition("accepted revision", ["test-input"]))
    task = db.get_task(conn, identifier)
    assert task["status"] == "PLANNED" and task["blocker"] is None
    assert task["depends_on"] == ["test-input"]


@pytest.mark.parametrize("reason", ["[AgentKit execution limit] max_turns reached", "Needs user decision"])
def test_concrete_execution_holds_survive_replan(conn, project_root, reason):
    identifier = planning.put(project_root, definition())
    db.set_status(conn, identifier, "NEEDS_REPLAN", actor="scheduler")
    db.update_task(conn, identifier, blocker=reason, blocked_meta="{bad metadata}")
    planning.put(project_root, definition("new accepted description"))
    task = db.get_task(conn, identifier)
    assert task["status"] == "PLANNED" and task["blocker"] == reason
    assert task["blocked_meta"] == "{bad metadata}"


def test_gate_environment_must_be_prepared_before_task_definition(project_root):
    path = project_root / ".ai/project.yaml"
    config = yaml.safe_load(path.read_text())
    config["environment_profiles"] = {"static": {"tools": []}}
    config["environment_roles"] = {"backend-builder": "static"}
    path.write_text(yaml.safe_dump(config))
    task = definition()
    task.role = "backend-builder"
    with pytest.raises(ValueError, match="No environment profile covers gate fast"):
        planning.put(project_root, task)
    assert not spec.load(project_root)
    config["environment_gates"] = {"fast": "static"}
    path.write_text(yaml.safe_dump(config))
    assert planning.put(project_root, task) > 0
