"""A monitor fault is AgentKit's problem, never the worker's.

The monitor that owns a worker writes heartbeats and events to SQLite. A busy
database or a bug in one of its checks must not terminate a healthy worker,
invent an exit code, or consume one of the task's attempts.
"""

import json
import sqlite3
import sys

from agentkit import db, processes, runner


def _launch(conn, root, marker, *, seconds=2.0):
    task = db.create_task(conn, title="Healthy worker", owned_paths=["services/retry.py"],
                          expected_write=["services/retry.py"], status="RUNNING",
                          worktree=str(root), generation=1)
    script = (f"import pathlib,sys,time;time.sleep({seconds});"
              "pathlib.Path(sys.argv[1]).write_text('finished');sys.exit(0)")
    launch = {"argv": [sys.executable, "-c", script, str(marker)], "cwd": str(root), "env": {}}
    process = conn.execute(
        "INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at) VALUES(?,?,?,?,?,?)",
        ("worker", "codex", task, 1, json.dumps(launch), db.utcnow()),
    ).lastrowid
    return task, process


def _kinds(conn, task):
    return [row["kind"] for row in conn.execute("SELECT kind FROM events WHERE task_id=?", (task,))]


def test_a_locked_database_skips_a_heartbeat_instead_of_stopping_the_worker(
        project_root, conn, monkeypatch, tmp_path):
    marker = tmp_path / "finished"
    task, process = _launch(conn, project_root, marker)
    real = db.heartbeat
    calls = []

    def locked_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "heartbeat", locked_once)
    assert runner.run(project_root, process) == 0
    assert calls, "the heartbeat must have been attempted"
    assert marker.read_text() == "finished"
    row = processes.get(conn, process)
    assert row["exit_code"] == 0 and row["status"] == "FINISHED"
    current = db.get_task(conn, task)
    assert current["status"] == "REVIEW" and current["attempts"] == 0
    assert "monitor_error" not in _kinds(conn, task)


def test_an_unexpected_monitor_error_leaves_the_worker_running_and_holds_the_task(
        project_root, conn, monkeypatch, tmp_path):
    from agentkit import monitor_audit
    marker = tmp_path / "finished"
    task, process = _launch(conn, project_root, marker)

    def broken(*args, **kwargs):
        raise RuntimeError("simulated audit bug")

    monkeypatch.setattr(monitor_audit, "check", broken)
    assert runner.run(project_root, process) == 0
    assert marker.read_text() == "finished"
    row = processes.get(conn, process)
    assert row["exit_code"] == 0
    assert "simulated audit bug" in row["error"]
    current = db.get_task(conn, task)
    assert current["status"] == "BLOCKED" and current["attempts"] == 0
    assert "simulated audit bug" in current["blocker"]
    kinds = _kinds(conn, task)
    assert "monitor_error" in kinds and "recovery_decision" not in kinds


def test_a_fault_after_the_worker_exits_records_its_real_exit_code(
        project_root, conn, monkeypatch, tmp_path):
    marker = tmp_path / "finished"
    task, process = _launch(conn, project_root, marker, seconds=0.5)

    def broken(*args, **kwargs):
        raise RuntimeError("simulated completion bug")

    monkeypatch.setattr(runner, "finish_worker", broken)
    assert runner.run(project_root, process) == 0
    assert marker.read_text() == "finished"
    row = processes.get(conn, process)
    assert row["exit_code"] == 0 and row["ended_at"]
    current = db.get_task(conn, task)
    assert current["status"] == "BLOCKED" and current["attempts"] == 0
    assert "recovery_decision" not in _kinds(conn, task)


def test_final_bookkeeping_survives_a_busy_database(project_root, conn, monkeypatch, tmp_path):
    marker = tmp_path / "finished"
    task, process = _launch(conn, project_root, marker, seconds=0.5)
    real = processes.update
    refused = []

    def busy_once_at_exit(connection, identifier, **fields):
        if "ended_at" in fields and not refused:
            refused.append(fields)
            raise sqlite3.OperationalError("database is locked")
        return real(connection, identifier, **fields)

    monkeypatch.setattr(processes, "update", busy_once_at_exit)
    monkeypatch.setattr(runner, "BUSY_RETRY_DELAYS", (0.0, 0.0))
    assert runner.run(project_root, process) == 0
    assert refused
    row = processes.get(conn, process)
    assert row["ended_at"] and row["exit_code"] == 0 and row["status"] == "FINISHED"
    assert db.get_task(conn, task)["status"] == "REVIEW"
