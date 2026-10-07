"""Only public messages and tool labels reach CLI/browser views."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from agentkit import dashboard, db, public_activity, windows

SECRET = "sk-ant-" + "a" * 40


def write(root, identifier, events):
    path = root / ".ai" / "runtime" / f"process-{identifier}" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"channel": "stdout", "at": db.utcnow(),
        "text": json.dumps(event)}) + "\n" for event in events), encoding="utf-8")
    return path


def public(text):
    return {"type": "assistant", "message": {"id": "one", "content": [
        {"type": "text", "text": text}, {"type": "thinking", "thinking": "PRIVATE_REASONING"},
        {"type": "tool_use", "name": "Read", "input": {"PRIVATE_ARGUMENTS": SECRET}}]}}


def test_provider_messages_redacted_multiline_and_hidden_payloads_excluded(tmp_path):
    write(tmp_path, 1, [public("Checking the adapter.\nAPI_KEY=" + SECRET),
        {"type": "item.completed", "item": {"id": "two", "type": "agent_message", "text": "Implementing the change."}},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_REASONING"}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "PRIVATE_TOOL_OUTPUT"}},
        {"type": "result", "result": "Finished the source change."}])
    result = public_activity.read(tmp_path, 1)
    encoded = json.dumps(result)
    assert result["status"] == "ok"
    assert "PRIVATE" not in encoded and SECRET not in encoded
    assert "Checking the adapter.\nAPI_KEY=[redacted]" in [message["text"] for message in result["messages"]]
    assert "Implementing the change." in encoded and "Finished the source change." in encoded
    assert "Read" in encoded and "command" in encoded


def test_repeated_snapshots_and_partial_json_are_not_fake_messages(tmp_path):
    path = write(tmp_path, 1, [public("Working."), public("Working.")])
    with path.open("ab") as stream:
        stream.write(b'{"channel":"stdout","text":"unfinished')
    result = public_activity.read(tmp_path, 1)
    assert [m["text"] for m in result["messages"] if m["kind"] == "message"] == ["Working."]
    assert result["partial"]
    assert result == public_activity.read(tmp_path, 1)


def test_tail_and_text_are_bounded_and_secret_after_limit_not_exposed(tmp_path):
    write(tmp_path, 1, [public("x" * 14_000 + SECRET) for _ in range(100)])
    result = public_activity.read(tmp_path, 1)
    assert result["partial"] and len(result["messages"]) <= public_activity.MAX_MESSAGES
    assert all(len(message["text"]) <= public_activity.MAX_TEXT for message in result["messages"])
    assert SECRET not in json.dumps(result)


def test_stderr_and_unknown_records_are_never_raw_console_output(tmp_path):
    path = write(tmp_path, 1, [])
    path.write_text(json.dumps({"channel": "stderr", "text": SECRET}) + "\n" +
                    json.dumps({"type": "unknown", "text": "PRIVATE"}) + "\n")
    assert public_activity.read(tmp_path, 1)["messages"] == []


def test_activity_http_numeric_recorded_ids_and_local_origin_only(project_root, conn):
    conn.execute("INSERT INTO processes(id,purpose,provider,status,started_at,launch_json) "
                 "VALUES(1,'worker','codex','FINISHED',?,'{}')", (db.utcnow(),))
    conn.commit()
    write(project_root, 1, [public('<script>alert("public")</script>')])
    before = list(conn.iterdump())
    files = set((project_root / ".ai").rglob("*"))
    server = dashboard.make_server(project_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def request(path, headers=None):
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                client.request("GET", path, headers=headers or {})
                response = client.getresponse()
                return response.status, response.read()
            finally:
                client.close()
        status, body = request("/api/activity/1")
        assert status == 200 and "alert" in json.loads(body)["messages"][0]["text"]
        for path in ("/api/activity/2", "/api/activity/../.env", "/api/activity/1/extra", "/api/activity/-1", "/api/activity/999999999999999999"):
            assert request(path)[0] == 404
        assert request("/api/activity/1", {"Origin": "https://remote.example"})[0] == 403
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    assert list(conn.iterdump()) == before
    assert set((project_root / ".ai").rglob("*")) == files


def test_symlink_escape_never_served(tmp_path):
    root = tmp_path / "project"
    folder = root / ".ai" / "runtime" / "process-1"
    folder.mkdir(parents=True)
    outside = tmp_path / "private.jsonl"
    outside.write_text(json.dumps(public("PRIVATE")) + "\n")
    try:
        (folder / "events.jsonl").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert public_activity.read(root, 1)["status"] == "missing"


def test_cli_viewer_prints_public_messages_once_while_worker_active(tmp_path, monkeypatch):
    write(tmp_path, 1, [public("Inspecting the scoped source.")])
    process = {"id": 1, "role": "builder", "model": "sol", "status": "RUNNING", "task_id": 1,
               "monitor_alive": True, "child_alive": True}
    active = {"status": "ok", "processes": [process], "tasks": [], "external_managers": []}
    stopped = {**active, "processes": []}
    snapshots = iter([active, active, stopped])
    monkeypatch.setattr(windows.live, "render", lambda frame: "Task #1 source change")
    shown = []
    assert windows.watch(tmp_path, 1, reader=lambda *args: next(snapshots), sleeper=lambda seconds: None,
                         output=shown.append) == 0
    assert shown.count("[message] Inspecting the scoped source.") == 1
    assert not any("PRIVATE" in frame for frame in shown)
