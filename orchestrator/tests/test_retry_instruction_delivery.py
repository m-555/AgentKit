"""Fresh retries receive current instructions rather than stale checkpoint advice."""
from agentkit import briefs, checkpoints, db, repo


def test_brief_shows_current_instruction_and_preserves_completed_notes():
    text = briefs.render({"task": {"id": 1, "title": "retry", "description": "goal", "kind": "TEST_ONLY",
                                  "status": "READY", "role": "backend-tester",
                                  "next_action": "Use python -S; keep assertions"},
                          "last_semantic_checkpoint": {"payload": {
                              "completed": ["51 test cases already authored"],
                              "next_action": "Old advice: request investigation"}}})
    assert "Use python -S; keep assertions" in text
    assert "51 test cases already authored" in text
    assert "Old advice" not in text


def test_recovery_packet_prefers_current_task_instruction(project_root, conn):
    task = db.create_task(conn, title="retry", owned_paths=["services/media.py"],
                          worktree=str(project_root), base_sha=repo.head_commit(project_root))
    checkpoints.write_mechanical(conn, project_root, project_root, task, "worker_exit")
    checkpoints.write_semantic(conn, task, {"completed": ["source already done"],
                                           "next_action": "Old advice"})
    db.update_task(conn, task, next_action="Only host commit and gate remain")
    before = db.latest_checkpoint(conn, task, kind="semantic")["payload"]
    packet = checkpoints.recover(conn, project_root, project_root, task)
    assert packet["semantic"]["next_action"] == "Only host commit and gate remain"
    assert packet["semantic"]["completed"] == ["source already done"]
    assert before["next_action"] == "Old advice"
    assert "Only host commit and gate remain" in checkpoints.render_recovery(packet)
