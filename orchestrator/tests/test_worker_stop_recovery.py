"""Budget and crash stops cannot silently start another paid worker."""
import json

from agentkit import db, processes, repo, runner, supervisor


def test_separate_worker_crash_requires_planner(conn, project_root, make_task):
    path = project_root / ".ai/project.yaml"
    with path.open("a") as stream:
        stream.write("\nworkflow:\n  mode: separate-tasks\n")
    task = make_task("builder", ["services/media.py"], worktree=str(project_root), base_sha=repo.head_commit(project_root), generation=1)
    runner.finish_worker(conn, project_root, {"task_id": task, "generation": 1, "provider": "claude-code", "launch_json": "{}"}, 1, "worker crashed")
    assert db.get_task(conn, task)["status"] == "FAILED"
    assert db.get_task(conn, task)["attempts"] == 1


def test_confirmed_wsl_exit_preserves_original_budget_stop(conn, project_root, make_task, monkeypatch):
    from agentkit import transport_ownership
    task = make_task("builder", ["services/media.py"], worktree=str(project_root), base_sha=repo.head_commit(project_root), generation=1)
    reason = "[AgentKit execution limit] error_max_turns"
    conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,status,pid,child_pid,launch_json,started_at,error,child_launch_state) VALUES('worker','claude-code',?,1,'FAILED',111,112,?,?,?,'WSL_UNCONFIRMED')", (task,json.dumps({"env":{"AGENTKIT_TRANSPORT":"wsl"}}),db.utcnow(),reason))
    monkeypatch.setattr(supervisor, "pid_alive", lambda _: False)
    monkeypatch.setattr(processes, "owning", lambda connection: [dict(connection.execute("SELECT * FROM processes").fetchone())])
    monkeypatch.setattr(transport_ownership, "recover_exit", lambda *_: True)
    monkeypatch.setattr(transport_ownership, "exit_confirmed", lambda *_: True)
    supervisor.recover_monitors(conn, project_root)
    assert db.get_task(conn, task)["status"] == "BLOCKED"
    assert db.get_task(conn, task)["attempts"] == 0
    assert processes.get(conn,1)["error"] == reason
