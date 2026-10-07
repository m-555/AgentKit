"""Retry gates only on unchanged reviewed source; no provider sessions."""
import pytest

from agentkit import db, gates, integration_retry, integrator, mcp_server, repo, statemachine
from tests.conftest import commit_all
from tests.test_workflow import reviewed_change


def failed(conn, project, project_root):
    task_id, work, head = reviewed_change(conn, project, project_root)
    db.set_status(conn, task_id, "FAILED", actor="integrator", cause="environment failed")
    db.update_task(conn, task_id, blocker="combined_gate: missing test dependency")
    return task_id, work, head


def test_retry_preserves_commit_review_and_runs_full_gate(conn, project, project_root, monkeypatch):
    task_id, work, head = failed(conn, project, project_root)
    integration_retry.authorize(conn, project, task_id, "Installed missing test dependency")
    task = db.get_task(conn, task_id)
    assert task["status"] == "INTEGRATION_READY"
    assert task["generation"] == 0
    assert repo.head_commit(work) == head
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    seen = []
    def run(project, level, **kwargs):
        seen.append(level)
        return gates.GateResult(level, True)
    monkeypatch.setattr(gates, "run_gate", run)
    assert integrator.merge_one(conn, project, task).ok
    assert seen == ["full"]
    assert db.get_task(conn, task_id)["status"] == "DONE"


@pytest.mark.parametrize("change", ["dirty", "new_commit", "invalid_review", "wrong_failure"])
def test_changed_or_unapproved_source_stays_failed(conn, project, project_root, change):
    task_id, work, _ = failed(conn, project, project_root)
    if change in ("dirty", "new_commit"):
        (work / "services/retry.py").write_text("VALUE = 'unreviewed'\n")
        if change == "new_commit":
            commit_all(work, "unreviewed edit")
    elif change == "invalid_review":
        from agentkit import reviews
        reviews.invalidate(conn, task_id, repo.head_commit(work), "changed scope")
    else:
        db.update_task(conn, task_id, blocker="review rejected")
    with pytest.raises(ValueError):
        integration_retry.authorize(conn, project, task_id, "Environment repaired")
    assert db.get_task(conn, task_id)["status"] == "FAILED"


def test_worker_cannot_authorize_retry(conn, project, project_root, monkeypatch):
    task_id, _, _ = failed(conn, project, project_root)
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setenv("AGENTKIT_TASK", str(task_id))
    with pytest.raises(PermissionError):
        mcp_server.integration_retry(task_id, "Worker cannot release own approval")


def test_agent_state_actor_cannot_skip_review():
    with pytest.raises(statemachine.TransitionError):
        statemachine.validate("FAILED", "INTEGRATION_READY", "agent")


def test_recovery_can_queue_approved_retry_but_cannot_merge_before_ack(
        conn, project, project_root):
    from agentkit import jobs, manager_state
    job = jobs.create(project_root, "recovery-retry", "Keep approved source")
    task_id, work, head = failed(conn, project, project_root)
    db.update_task(conn, task_id, job_id=job["id"])
    manager_state.record_outage(conn, job["id"], "codex", "manager lease expired")
    integration_retry.authorize(conn, project, task_id, "Dependency lock repaired")
    task = db.get_task(conn, task_id)
    assert task["status"] == "INTEGRATION_READY"
    assert task["generation"] == 0
    assert repo.head_commit(work) == head
    assert manager_state.pending(conn, job["id"])
    result = integrator.merge_one(conn, project, task)
    assert not result.ok
    assert "recovery" in result.summary()
    assert db.get_task(conn, task_id)["status"] == "INTEGRATION_READY"
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
