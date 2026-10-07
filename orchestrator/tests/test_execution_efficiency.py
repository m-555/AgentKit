"""Regressions for observed wasted-work and sandbox/ownership failures
no models."""

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit import db, hooks_cli, providers, reconcile, repo, runner, supervisor
from agentkit.run_limits import Meter, limits


def test_precommit_reads_live_authority_without_database_writes(
    project_root, conn, monkeypatch, capsys
):
    task = db.create_task(
        conn, title="Owned source", owned_paths=["services/retry.py"], status="RUNNING"
    )
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("AGENTKIT_TASK", str(task))
    before = list(conn.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("precommit must not migrate or write the authoritative database")

    monkeypatch.setattr(db, "connect", forbidden)
    (project_root / "services/retry.py").write_text("VALUE = 2\n")
    subprocess.run(["git", "add", "services/retry.py"], cwd=project_root, check=True)
    assert hooks_cli.handle_pre_commit({}) == 0
    (project_root / "services/media.py").write_text("VALUE = 3\n")
    subprocess.run(["git", "add", "services/media.py"], cwd=project_root, check=True)
    assert hooks_cli.handle_pre_commit({}) == 1
    assert "Commit rejected" in capsys.readouterr().err
    assert list(conn.iterdump()) == before


def test_finished_blocked_worker_preserves_work_and_requires_manager(
    project_root, conn, monkeypatch
):
    task = db.create_task(
        conn,
        title="Commit blocked",
        owned_paths=["services/retry.py"],
        worktree=str(project_root),
        generation=1,
        status="BLOCKED",
        blocker="read-only commit authority",
    )
    db.update_task(conn, task, blocker="read-only commit authority")
    (project_root / "services/retry.py").write_text("preserved = True\n")
    monkeypatch.setattr(
        runner.gates,
        "run_gate",
        lambda *a, **k: pytest.fail("blocked task must not rerun checks"),
    )
    runner.finish_worker(
        conn, project_root, {"task_id": task, "generation": 1, "provider": "claude-code"}, 0, ""
    )
    row = db.get_task(conn, task)
    assert row["status"] == "BLOCKED" and row["blocker"] == "read-only commit authority"
    assert (project_root / "services/retry.py").read_text() == "preserved = True\n"


def test_monitor_reuses_passing_gate_for_same_clean_commit(project_root, conn, monkeypatch):
    task = db.create_task(
        conn,
        title="Already checked",
        owned_paths=["services/retry.py"],
        worktree=str(project_root),
        generation=1,
        status="RUNNING",
    )
    head = repo.head_commit(project_root)
    db.record_gate(conn, task, "fast", head, True, "already passed")
    monkeypatch.setattr(runner.gates, "run_gate", lambda *a, **k: pytest.fail("duplicate gate"))
    runner.finish_worker(
        conn, project_root, {"task_id": task, "generation": 1, "provider": "claude-code"}, 0, ""
    )
    assert db.get_task(conn, task)["status"] == "REVIEW"


def test_execution_limit_parks_without_provider_cooldown(project_root, conn):
    task = db.create_task(
        conn,
        title="Too many calls",
        owned_paths=["services/retry.py"],
        worktree=str(project_root),
        generation=1,
        status="RUNNING",
    )
    runner.finish_worker(
        conn,
        project_root,
        {"task_id": task, "generation": 1, "provider": "codex"},
        -15,
        "[AgentKit execution limit] max_tool_calls reached",
    )
    row = db.get_task(conn, task)
    assert row["status"] == "BLOCKED" and row["attempts"] == 0
    assert not conn.execute("SELECT 1 FROM provider_state").fetchone()


def test_meter_deduplicates_tools_and_does_not_count_thinking_twice():
    meter = Meter()
    event = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "usage": {"output_tokens": 100, "output_tokens_details": {"thinking_tokens": 90}},
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"secret": "private"}}
            ],
        },
    }
    meter.observe(json.dumps(event))
    meter.observe(json.dumps(event))
    assert meter.output == 100 and len(meter.tools) == 1
    assert "private" not in repr(meter)
    assert meter.exceeded({"max_output_tokens": 100}, 0)
    assert not meter.exceeded({"max_tool_calls": 2}, 0)
    meter.observe(json.dumps({"type": "result", "subtype": "error_max_turns", "num_turns": 16}))
    assert meter.stop_reason == "error_max_turns" and meter.turns == 16


def test_codex_call_items_count_once_across_updates():
    meter = Meter()
    for kind in ("item.started", "item.completed"):
        meter.observe(
            json.dumps({"type": kind, "item": {"id": "c1", "type": "command_execution"}})
        )
    assert len(meter.tools) == 1
    assert meter.turns is None  # A CLI user turn is not a model-loop count.


@pytest.mark.parametrize("value", [True, 0, -1, "16", 1.5])
def test_invalid_limits_rejected(value):
    with pytest.raises(ValueError):
        limits(SimpleNamespace(raw={"execution_limits": {"max_turns": value}}))


def test_paused_supervisor_makes_no_model_or_database_calls(project_root, monkeypatch):
    (project_root / ".ai/project.yaml").write_text("execution_paused: true\n")
    monkeypatch.setattr(
        db, "connect", lambda *a: pytest.fail("paused supervisor touched runtime")
    )
    assert "paused" in supervisor.tick(project_root)[0]


@pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc"), reason="Linux process lifecycle"
)
def test_actual_zombie_is_stopped_but_live_process_is_alive():
    assert reconcile.pid_alive(os.getpid())
    child = os.fork()
    if child == 0:
        os._exit(0)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.01)
        assert state == "Z" and not reconcile.pid_alive(child)
    finally:
        os.waitpid(child, 0)


def test_healthy_paid_availability_probe_is_not_repeated(project, conn, monkeypatch):
    providers.clear_cooldown(conn, "claude-code")
    adapter = SimpleNamespace(
        availability_requires_inference=True,
        account_id=lambda: "default",
        check_availability=lambda: pytest.fail("healthy paid ping"),
    )
    monkeypatch.setattr(supervisor.adapters, "get", lambda name, project=None: adapter)
    supervisor.refresh_accounts(conn, project, ["claude-code"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX group ownership")
def test_stopping_owned_worker_also_closes_descendant_output_pipe():
    import sys

    from agentkit.worker_stop import stop
    child = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,time,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print('ready',flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        stop(child)
        child.communicate(timeout=3)
        assert child.returncode != 0
    finally:
        stop(child, force=True)
        child.wait(timeout=3)


def test_real_monitor_stops_dummy_worker_at_tool_bound_without_quota(project_root, conn):
    import sys

    from agentkit import processes
    path = project_root / ".ai/project.yaml"
    path.write_text(path.read_text() + "\nexecution_limits: {max_tool_calls: 1, max_runtime_seconds: 5}\n")
    identifier = db.create_task(conn, title="Bounded dummy", owned_paths=["services/retry.py"],
                                status="RUNNING", worktree=str(project_root), generation=1)
    script = "from pathlib import Path;import json,time;Path('services/retry.py').write_text('preserved = True\\n');print(json.dumps({'type':'item.completed','item':{'id':'one','type':'command_execution'}}),flush=True);time.sleep(60)"
    launch = {"argv": [sys.executable, "-c", script], "cwd": str(project_root), "env": {}, "stdin_text": "x" * 20000}
    process_id = conn.execute(
        "INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at) VALUES(?,?,?,?,?,?)",
        ("worker", "codex", identifier, 1, json.dumps(launch), db.utcnow()),
    ).lastrowid
    started = time.monotonic()
    assert runner.run(project_root, process_id) != 0
    assert time.monotonic() - started < 15
    row = db.get_task(conn, identifier)
    assert row["status"] == "BLOCKED" and "max_tool_calls" in row["blocker"]
    assert row["attempts"] == 0 and not conn.execute("SELECT 1 FROM provider_state").fetchone()
    assert "preserved" in (project_root / "services/retry.py").read_text()
    assert processes.get(conn, process_id)["ended_at"]


def test_mcp_requeue_refuses_terminal_row_with_live_owner(project_root, conn, monkeypatch):
    from agentkit import mcp_server
    identifier = db.create_task(conn, title="Preserved", status="BLOCKED", generation=1)
    conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,status,pid,child_pid,child_launch_state,ended_at,exit_code,launch_json,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("worker", "codex", identifier, 1, "FINISHED", os.getpid(), os.getpid(), "EXITED", db.utcnow(), 0, "{}", db.utcnow()))
    monkeypatch.setattr(mcp_server, "_root", lambda: project_root)
    monkeypatch.setattr(mcp_server, "_require_planner", lambda *a: None)
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="still owns"):
        mcp_server.task_requeue(identifier, "Continue")
    assert list(conn.iterdump()) == before
