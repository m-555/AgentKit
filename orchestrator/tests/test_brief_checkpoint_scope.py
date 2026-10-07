"""Short worker briefs retain evidence without replaying stale progress or logs."""
from agentkit import briefs, db, repo


def task_and_project(conn, project, monkeypatch):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    monkeypatch.setattr(briefs, "_job_memory", lambda *a: None)
    task = db.create_task(conn, title="Tiny", spec_id="tiny", role="backend-builder", status="READY",
                          owned_paths=["services/media.py"], expected_write=["services/media.py"])
    return task


def test_stale_progress_is_not_sent_as_current_instruction(conn, project, monkeypatch):
    task = task_and_project(conn, project, monkeypatch)
    db.write_checkpoint(conn, task, {"completed": ["OLD progress claim"], "decisions": ["keep old API"]},
                        kind="semantic", reason="old session", head_sha="old-head", generation=1)
    db.write_checkpoint(conn, task, {"kind": "mechanical", "head_sha": repo.head_commit(project.root)},
                        kind="mechanical", reason="host refresh")
    result = briefs.build(conn, project, task)
    rendered = briefs.render(result)
    assert "OLD progress claim" not in rendered
    assert "keep old API" in rendered
    assert db.latest_checkpoint(conn, task, kind="semantic")["payload"]["completed"] == ["OLD progress claim"]


def test_gate_logs_stay_durable_without_replaying_into_brief(conn, project, monkeypatch):
    task = task_and_project(conn, project, monkeypatch)
    summary = "historical diagnostic " * 200
    db.write_checkpoint(conn, task, {"kind": "mechanical", "head_sha": "exact-head",
                        "gates_run": [{"level": "fast", "passed": False, "head_sha": "exact-head", "summary": summary}]},
                        kind="mechanical", reason="worker exit")
    result = briefs.build(conn, project, task)
    assert "historical diagnostic" not in str(result["last_checkpoint"])
    assert result["last_checkpoint"]["gates_run"][0]["passed"] is False
    assert db.latest_checkpoint(conn, task, kind="mechanical")["payload"]["gates_run"][0]["summary"] == summary


def test_recovery_uses_latest_gate_without_repeating_resolved_failures(conn, project):
    from agentkit import checkpoints
    task = db.create_task(conn, title="resume", spec_id="resume", status="READY")
    mechanical = {"gates_run": [
        {"level": "fast", "passed": True, "head_sha": "new"},
        {"level": "fast", "passed": False, "head_sha": "old", "summary": "resolved dependency"},
    ]}
    semantic = checkpoints._reconstruct(conn, task, mechanical, {})
    assert semantic["blockers"] == []
    rendered = checkpoints.render_recovery({"mechanical": mechanical, "semantic": semantic, "reconstructed": True})
    assert "fast PASS at new" in rendered
    assert "FAIL" not in rendered
