"""A successfully repaired environment must not send workers its old failure."""
import pytest

from agentkit import checkpoints, db, gates, repo, worker_preparation


def test_success_supersedes_historical_setup_gate(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    failure = "gate `worktree_setup` skipped: insufficient disk space"
    task_id = db.create_task(conn, title="repair", status="READY", blocker=failure,
                             next_action=failure)
    db.update_task(conn, task_id, blocker=failure, next_action=failure)
    task = db.get_task(conn, task_id)
    head = repo.head_commit(project.root)
    db.record_gate(conn, task_id, "worktree_setup", head, False, failure)
    checkpoints.write_mechanical(conn, project.root, project.root, task_id, "old_setup_failure")
    worker_preparation.record_ready(conn, project, task, project.root,
                                    gates.GateResult("worktree_setup", True, []))
    recovered = checkpoints.recover(conn, project.root, project.root, task_id)
    assert "insufficient disk space" not in checkpoints.render_recovery(recovered)
    current = db.get_task(conn, task_id)
    assert current["blocker"] is None and current["next_action"] is None
    historical = conn.execute("SELECT passed FROM gate_results WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    assert [row[0] for row in historical] == [1]
    import json
    old = conn.execute("SELECT payload FROM checkpoints WHERE task_id=? AND reason=?",
                       (task_id, "old_setup_failure")).fetchone()
    assert json.loads(old[0])["gates_run"][0]["passed"] is False
    assert failure in json.loads(old[0])["gates_run"][0]["summary"]


def test_failed_setup_cannot_clear_real_blocker(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    task_id = db.create_task(conn, title="blocked", status="READY", blocker="missing authorization")
    db.update_task(conn, task_id, blocker="missing authorization")
    with pytest.raises(ValueError, match="Cannot record failed"):
        worker_preparation.record_ready(conn, project, db.get_task(conn, task_id), project.root,
                                        gates.GateResult("worktree_setup", False, []))
    assert db.get_task(conn, task_id)["blocker"] == "missing authorization"
