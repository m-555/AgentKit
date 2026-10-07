"""A deferred launch must still deliver the manager's repair on the next attempt."""
import pytest

from agentkit import checkpoints, db, repo, scheduler


@pytest.mark.parametrize("phase", ["preflight", "launch"])
@pytest.mark.parametrize("instruction", [None, "REQUIRED: reject wrong provider identity"])
def test_scheduler_hold_preserves_instruction_in_recovery_packet(project_root, conn, monkeypatch, phase, instruction):
    identifier = db.create_task(conn, title="Repair", status="READY", next_action=instruction,
                                owned_paths=["services/media.py"], worktree=str(project_root),
                                base_sha=repo.head_commit(project_root))
    checkpoints.write_mechanical(conn, project_root, project_root, identifier, "worker_exit")
    checkpoints.write_semantic(conn, identifier, {"completed": ["source committed"],
                                                 "next_action": "Old instruction: only rerun gate"})
    task = db.get_task(conn, identifier)
    reason = "previous monitor exit not yet confirmed"
    report = scheduler.SchedulerReport(unassignable=[(task, reason)] if phase == "preflight" else [])
    plan = scheduler.LaunchPlan(task, "claude-code", project_root, 1, "selected")
    monkeypatch.setattr(scheduler, "plan", lambda *a, **k: ([] if phase == "preflight" else [plan], report))
    monkeypatch.setattr(scheduler, "launch", lambda *a, **k: (False, reason))
    scheduler._run_once(project_root)
    current = db.get_task(conn, identifier)
    assert current["status"] == "READY" and current["blocker"] == reason
    packet = checkpoints.recover(conn, project_root, project_root, identifier)
    assert packet["semantic"]["next_action"] == (instruction or reason)
    assert "Old instruction" not in checkpoints.render_recovery(packet)
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0


def test_concurrent_new_instruction_wins_over_scheduler_snapshot(project_root, conn, monkeypatch):
    identifier = db.create_task(conn, title="Repair", status="READY", next_action="old repair")
    task = db.get_task(conn, identifier)
    plan = scheduler.LaunchPlan(task, "claude-code", project_root, 1, "selected")
    monkeypatch.setattr(scheduler, "plan", lambda *a, **k: ([plan], scheduler.SchedulerReport()))
    def refused(*args, **kwargs):
        db.update_task(conn, identifier, next_action="new authenticated repair")
        return False, "temporary guard failure"
    monkeypatch.setattr(scheduler, "launch", refused)
    scheduler._run_once(project_root)
    assert db.get_task(conn, identifier)["next_action"] == "new authenticated repair"
