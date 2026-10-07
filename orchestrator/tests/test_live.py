"""Actual process-stream fixtures verify safe, read-only visibility."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentkit import db, live

STAMP = "2026-10-01T08:00:00+00:00"
SESSION = "0199ed18-12ab-7345-a456-123456789abc"
SECRET = "sk-ant-" + "a" * 40
PRIVATE = "HIDDEN_PROMPT_REASONING_OR_OUTPUT"


def process(conn, *, task_id=None, purpose="worker", provider="codex", status="RUNNING",
            pid=None, child_pid=None, session=SESSION, launch=None):
    launch = launch or {"argv": ["codex", "exec", PRIVATE], "cwd": "C:/work/agent",
                        "stdin_text": PRIVATE, "env": {"API_TOKEN": SECRET,
                        "AGENTKIT_MODEL": "gpt-6.1-sol", "AGENTKIT_MODEL_EFFORT": "xhigh"}}
    return conn.execute("INSERT INTO processes(purpose,task_id,job_id,provider,status,pid,"
                        "child_pid,session_token,launch_json,started_at,heartbeat_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (purpose, task_id, "demo-job", provider, status, pid, child_pid,
                         session, json.dumps(launch), STAMP, STAMP)).lastrowid


def stream(root, identifier, messages):
    folder = root / ".ai" / "runtime" / f"process-{identifier}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "events.jsonl"
    path.write_text("".join(json.dumps({"at": STAMP, "channel": "stdout",
                                      "text": json.dumps(m)}) + "\n" for m in messages),
                    encoding="utf-8")
    return path


def test_worker_metadata_gate_and_actual_codex_tools(project_root, conn, make_task):
    task = make_task("Implement live view", ["src/live.py"], model="gpt-6.1-sol")
    identifier = process(conn, task_id=task, pid=os.getpid(), child_pid=os.getpid())
    db.record_gate(conn, task, "fast", "a" * 40, True, PRIVATE)
    stream(project_root, identifier, [
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "item.started", "item": {"type": "command_execution", "command": PRIVATE}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "gate_run",
                                               "arguments": {"secret": SECRET}}},
    ])
    before = list(conn.iterdump())
    files = set((project_root / ".ai").rglob("*"))
    view = live.snapshot(project_root)
    row = view["processes"][0]
    assert row["state"] == "RUNNING"
    assert row["monitor_alive"] and row["child_alive"]
    assert (row["model"], row["effort"], row["role"]) == ("gpt-6.1-sol", "xhigh", "implementer")
    assert row["session_id"] == SESSION
    assert row["worktree"] == "C:/work/agent"
    assert [e.get("name") for e in row["stream"]["activity"]] == [None, "command", "gate_run"]
    assert view["tasks"][0]["tests"]["passed"]
    assert "fast:PASS@aaaaaaaaaaaa" in live.render(view)
    serialized = json.dumps(view) + live.render(view)
    assert PRIVATE not in serialized and SECRET not in serialized
    assert "launch_json" not in serialized and "session_token" not in serialized
    assert list(conn.iterdump()) == before
    assert set((project_root / ".ai").rglob("*")) == files


def test_coordinator_and_reviewer_without_tasks(project_root, conn):
    for purpose in ("coordinator", "review"):
        identifier = process(conn, purpose=purpose, provider="claude-code", pid=os.getpid(),
                             launch={"cwd": "C:/control", "env": {"AGENTKIT_MODEL": "claude-opus-5-5",
                                     "AGENTKIT_MODEL_EFFORT": "xhigh"}, "stdin_text": PRIVATE})
        stream(project_root, identifier, [{"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": PRIVATE}, {"type": "text", "text": PRIVATE},
            {"type": "tool_use", "name": "Read", "input": {"file_path": PRIVATE}},
        ]}}])
    view = live.snapshot(project_root)
    assert view["tasks"] == []
    assert {p["role"] for p in view["processes"]} == {"coordinator", "review"}
    assert {p["model"] for p in view["processes"]} == {"claude-opus-5-5"}
    assert all(p["stream"]["activity"][0]["name"] == "Read" for p in view["processes"])
    assert PRIVATE not in json.dumps(view) + live.render(view)


@pytest.mark.parametrize(("provider", "messages", "name"), [
    ("codex", [{"type": "item.completed", "item": {"type": "file_change", "changes": PRIVATE}},
               {"type": "item.completed", "item": {"type": "reasoning", "text": PRIVATE}},
               {"type": "item.completed", "item": {"type": "agent_message", "text": PRIVATE}}],
     "file_change"),
    ("local-opencode", [{"type": "tool_use", "part": {"tool": "grep", "state": {
                        "input": PRIVATE, "output": SECRET}}}], "grep"),
    ("claude-code", [{"kind": "tool_use", "text": PRIVATE, "detail": {"tool_name": "Bash", "input": PRIVATE,
                     "output": SECRET}}, {"kind": "reasoning", "detail": PRIVATE}], "Bash"),
])
def test_vendor_and_normalized_stream_projection(project_root, conn, provider, messages, name):
    identifier = process(conn, provider=provider)
    stream(project_root, identifier, messages)
    view = live.snapshot(project_root)
    assert view["processes"][0]["stream"]["activity"][0]["name"] == name
    assert PRIVATE not in json.dumps(view) and SECRET not in live.render(view)


def test_metadata_redaction_controls_and_session_credentials(project_root, conn, make_task):
    task = make_task("API_TOKEN=" + SECRET + "\x1b[31m", [])
    identifier = process(conn, task_id=task, session="Bearer " + SECRET,
                         launch={"env": {"AGENTKIT_MODEL": SECRET,
                                 "AGENTKIT_MODEL_EFFORT": {"prompt": PRIVATE}},
                                 "cwd": "https://user:password123@example.com/work"})
    stream(project_root, identifier, [{"kind": "tool_use", "name": SECRET,
                                      "environment": {"PRIVATE_KEY": PRIVATE}}])
    view = live.snapshot(project_root)
    text = json.dumps(view) + live.render(view)
    assert SECRET not in text and PRIVATE not in text and "password123" not in text
    assert "\x1b" not in text
    assert "[redacted]" in text
    assert view["processes"][0]["session_id"] == "unknown"
    assert view["processes"][0]["effort"] == "unknown"


def test_malformed_partial_and_oversized_streams(project_root, conn):
    identifier = process(conn)
    path = stream(project_root, identifier, [{"kind": "tool_use", "name": "Read"}])
    with path.open("ab") as log:
        log.write(b"{invalid\nnull\n[1,2]\n" + b"[" * 1500 + b"]" * 1500 + b"\n")
        log.write(json.dumps({"type": [], "reasoning": PRIVATE}).encode() + b"\n")
        log.write(json.dumps({"type": "item.completed", "item": {"type": []}}).encode() + b"\n")
        log.write(b'{"at":"bad","channel":"stderr","text":"raw secret"}\n')
        log.write(b'{"kind":"tool_use","name":"Write"')
    result = live.snapshot(project_root)["processes"][0]["stream"]
    assert result["activity"][0]["name"] == "Read"
    assert result["skipped_records"] >= 5
    with path.open("wb") as log:
        log.write(b'{"prompt":"' + b"x" * (live.MAX_BYTES + 100) + b'"}\n')
        for i in range(10):
            log.write(json.dumps({"kind": "tool_use", "name": f"tool_{i}"}).encode() + b"\n")
    result = live.snapshot(project_root)["processes"][0]["stream"]
    assert result["partial"]
    assert [e["name"] for e in result["activity"]] == [f"tool_{i}" for i in range(4, 10)]


@pytest.mark.parametrize(("status", "pid", "child", "alive", "expected"), [
    ("STARTING", None, None, set(), "STARTING"),
    ("RUNNING", 10, 20, {10, 20}, "RUNNING"),
    ("RUNNING", 10, 20, set(), "CRASHED"),
    ("RUNNING", 10, 20, {20}, "ORPHANED"),
    ("FINISHED", 10, 20, set(), "STOPPED"),
    ("FINISHED", 10, 20, {10}, "TERMINAL_PID_ALIVE"),
    ("FINISHED", 10, 20, {20}, "TERMINAL_PID_ALIVE"),
    ("FINISHED", None, None, set(), "EXIT_UNCONFIRMED"),
    ("FAILED", 10, 20, set(), "FAILED"),
    ("FAILED", 10, 20, {20}, "TERMINAL_PID_ALIVE"),
])
def test_process_lifecycle_distinguishes_live_exit_and_crash(project_root, conn, monkeypatch,
                                                          status, pid, child, alive, expected):
    process(conn, status=status, pid=pid, child_pid=child)
    monkeypatch.setattr(live, "pid_alive", lambda candidate: candidate in alive)
    row = live.snapshot(project_root)["processes"][0]
    assert row["state"] == expected and row["status"] == status
    assert row["monitor_alive"] == (pid in alive if pid else None)
    assert row["child_alive"] == (child in alive if child else None)
    assert row["liveness_risk"] == (expected == "TERMINAL_PID_ALIVE")


def test_quota_wait_reset_is_observation_not_recovered_allowance(project_root, conn, make_task):
    task = make_task("Waiting worker", [], status="BLOCKED")
    db.update_task(conn, task, blocked_meta=json.dumps({
        "reason": "provider_usage_limit", "retry_at": STAMP, "raw_message": PRIVATE}))
    process(conn, task_id=task, status="FAILED")
    conn.execute("INSERT INTO provider_state(account_key,provider,status,retry_at,raw_message) "
                 "VALUES('codex:default','codex','COOLDOWN',?,?)", (STAMP, PRIVATE))
    for window, used, reset in (("five_hour", 0, STAMP), ("weekly", 100, None)):
        conn.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)",
                     ("codex:default", "main", window, used, reset, STAMP, "fixture"))
    view = live.snapshot(project_root)
    assert view["tasks"][0]["progress"] == "WAITING_QUOTA"
    assert view["providers"][0]["status"] == "COOLDOWN"
    assert view["quota_windows"][1]["resets_at"] is None
    text = live.render(view)
    assert "WAITING_QUOTA" in text and "weekly" in text and "reset=unknown" in text
    assert PRIVATE not in text + json.dumps(view)


def test_task_filter_precedes_row_limits_and_keeps_controls(project_root, conn, make_task):
    task = make_task("Old active task", [])
    worker = process(conn, task_id=task)
    control = process(conn, purpose="coordinator")
    other_task = make_task("Other worker", [])
    process(conn, task_id=other_task)
    conn.executemany("INSERT INTO tasks(title,created_at,updated_at) VALUES(?,?,?)",
                     [(f"Later {i}", STAMP, STAMP) for i in range(live.MAX_ROWS + 1)])
    view = live.snapshot(project_root, task_id=task)
    assert view["status"] == "ok"
    assert [t["id"] for t in view["tasks"]] == [task]
    assert {p["id"] for p in view["processes"]} == {worker, control}
    assert live.snapshot(project_root)["bounded"]


def test_missing_runtime_is_never_created(tmp_path):
    root = tmp_path / "missing"
    assert live.run(root, output=lambda text: None) == 1
    assert not root.exists()
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "project.yaml").write_text("name: demo\n", encoding="utf-8")
    assert live.snapshot(root)["status"] == "missing"
    assert not (root / ".ai" / "tasks.db").exists()


def test_existing_database_connection_refuses_writes(project_root, conn, monkeypatch):
    original = sqlite3.connect
    called = []

    def readonly(database_uri, **kwargs):
        called.append((database_uri, kwargs))
        connection = original(database_uri, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO meta VALUES('monitor-write','bad')")
        connection.rollback()  # Failed INSERT still opened Python's implicit transaction.
        return connection

    monkeypatch.setattr(live.sqlite3, "connect", readonly)
    assert live.snapshot(project_root)["status"] == "ok"
    assert called[0][0].endswith("?mode=ro") and called[0][1]["uri"]


def test_missing_tables_and_unreadable_database_are_safe(project_root, conn, monkeypatch):
    for table in ("processes", "provider_state", "quota_windows", "gate_results", "events"):
        conn.execute(f"DROP TABLE {table}")
    assert live.snapshot(project_root)["status"] == "ok"

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError(SECRET + PRIVATE)

    monkeypatch.setattr(live.sqlite3, "connect", unavailable)
    view = live.snapshot(project_root)
    assert view["status"] == "unavailable"
    assert SECRET not in live.render(view) and PRIVATE not in json.dumps(view)


def test_escaping_stream_is_not_read(project_root, conn, monkeypatch, tmp_path):
    identifier = process(conn)
    path = stream(project_root, identifier, [{"kind": "tool_use", "name": "unsafe"}])
    original = Path.resolve

    def escaped(candidate, *args, **kwargs):
        return tmp_path / "outside.jsonl" if candidate == path else original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped)
    result = live.snapshot(project_root)["processes"][0]["stream"]
    assert result == {"activity": [], "status": "missing"}


def test_follow_discovers_new_process_and_sleeps(project_root, conn):
    frames, delays = [], []

    def sleep(delay):
        delays.append(delay)
        process(conn, purpose="coordinator", pid=os.getpid())

    assert live.run(project_root, follow=True, json_output=True, max_iterations=2,
                    poll_seconds=0.2, sleeper=sleep, output=frames.append) == 0
    assert json.loads(frames[0])["processes"] == []
    assert json.loads(frames[1])["processes"][0]["role"] == "coordinator"
    assert delays == [0.2]


def test_follow_interrupt_exits_cleanly(project_root, conn):
    frames = []

    def interrupt(delay):
        raise KeyboardInterrupt

    assert live.run(project_root, follow=True, sleeper=interrupt, output=frames.append) == 0
    assert len(frames) == 1


@pytest.mark.parametrize("poll", [0, -1, 0.05, float("nan"), float("inf")])
def test_invalid_poll_never_spins(tmp_path, poll, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid poll must not touch runtime state")

    monkeypatch.setattr(live, "snapshot", forbidden)
    assert live.run(tmp_path, poll_seconds=poll, follow=True, output=lambda text: None) == 2


@pytest.mark.parametrize(("age", "pid", "released", "expected"), [
    (0, None, None, "ACTIVE"), (120, None, None, "STALE"),
    (0, 10, None, "CRASHED"), (120, 10, STAMP, "RELEASED"),
])
def test_external_manager_visibility_never_selects_auth_or_recovery_payloads(
        project_root, conn, monkeypatch, age, pid, released, expected):
    heartbeat = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
    conn.execute("INSERT INTO manager_leases(job_id,holder,token_hash,provider,model,effort,"
                 "session_ref,pid,ttl_seconds,takeover_grace,acquired_at,heartbeat_at,"
                 "released_at,release_reason,outage_recorded) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("external-job", "desktop-agent", SECRET, "codex", "gpt-6.1-sol", "xhigh",
                  SESSION, pid, 60, 30, STAMP, heartbeat, released, PRIVATE, 1))
    conn.execute("INSERT INTO manager_state(job_id,epoch,acknowledged_epoch,audit_epoch,"
                 "audit_at,ack_at,updated_at,audit_report,authorized_tasks,ack_evidence) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("external-job", 2, 1, 2, STAMP, STAMP, STAMP, PRIVATE, PRIVATE, SECRET))
    monkeypatch.setattr(live, "pid_alive", lambda candidate: False)
    original = sqlite3.connect
    queries = []

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(live.sqlite3, "connect", traced)
    view = live.snapshot(project_root)
    manager = view["external_managers"][0]
    assert manager["state"] == expected
    assert manager["session_id"] == SESSION and not manager["model_verified"]
    assert manager["recovery"]["epoch"] == 2
    assert manager["recovery"]["acknowledged_epoch"] == 1
    assert manager["recovery"]["audit_epoch"] == 2
    text = json.dumps(view) + live.render(view) + "\n".join(queries)
    assert PRIVATE not in text and SECRET not in text
    assert "token_hash" not in text and "audit_report" not in text and "ack_evidence" not in text


def test_requested_and_observed_model_metadata_are_distinct(project_root, conn):
    identifier = process(conn, pid="invalid-pid")
    conn.execute("UPDATE processes SET requested_model='gpt-6.1-sol',requested_effort='high',"
                 "observed_model='gpt-6-astra',observed_effort='xhigh',model_verified=0 WHERE id=?",
                 (identifier,))
    row = live.snapshot(project_root)["processes"][0]
    assert row["pid"] is None
    assert row["model"] == row["requested_model"] == "gpt-6.1-sol"
    assert row["effort"] == "high" and row["model_source"] == "requested"
    assert row["observed_model"] == "gpt-6-astra" and not row["model_verified"]
    conn.execute("UPDATE processes SET model_verified=1 WHERE id=?", (identifier,))
    row = live.snapshot(project_root)["processes"][0]
    assert row["model"] == "gpt-6-astra" and row["effort"] == "xhigh"
    assert row["model_source"] == "observed" and row["model_verified"]


@pytest.mark.parametrize("status", ["FINISHED", "FAILED"])
def test_terminal_rows_use_actual_liveness(project_root, conn, status):
    process(conn, status=status, pid=os.getpid(), child_pid=os.getpid())
    view = live.snapshot(project_root)
    row = view["processes"][0]
    assert row["status"] == status and row["state"] == "TERMINAL_PID_ALIVE"
    assert row["monitor_alive"] and row["child_alive"] and row["liveness_risk"]
    assert "TERMINAL_PID_ALIVE" in live.render(view)
    assert "liveness_risk=True" in live.render(view)
    assert PRIVATE not in live.render(view)


def test_old_schema_launch_metadata_still_works(project_root, conn):
    for name in ("requested_model", "requested_effort", "requested_profile", "observed_model",
                 "observed_effort", "model_verified"):
        conn.execute(f"ALTER TABLE processes DROP COLUMN {name}")
    conn.execute("DROP TABLE manager_state")
    conn.execute("DROP TABLE manager_leases")
    process(conn)
    view = live.snapshot(project_root)
    row = view["processes"][0]
    assert view["status"] == "ok" and view["external_managers"] == []
    assert row["requested_model"] == "gpt-6.1-sol" and row["requested_effort"] == "xhigh"
    assert row["observed_model"] == "unknown" and not row["model_verified"]


def test_missing_optional_blocked_metadata_does_not_hide_active_worker(project_root, conn, make_task):
    task = make_task("Older runtime", ["src/worker.py"])
    identifier = process(conn, task_id=task, pid=os.getpid(), child_pid=os.getpid())
    conn.execute("ALTER TABLE tasks DROP COLUMN blocked_meta")
    before = list(conn.iterdump())
    view = live.snapshot(project_root, process_id=identifier)
    assert view["status"] == "ok"
    assert view["processes"][0]["monitor_alive"]
    assert view["tasks"][0]["progress"] == "RUNNING"
    assert list(conn.iterdump()) == before
