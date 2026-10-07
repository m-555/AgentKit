"""Window identity and manager-only input fences, without launching an AI."""
from __future__ import annotations

import io
import json
import struct
import subprocess
import threading
import zlib
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from agentkit import (
    dashboard,
    dashboard_actions,
    terminal_frame,
    terminal_manager,
    terminal_mirror,
)


def record(root, **changes):
    value = {"version": 1, "key": "process-1", "hwnd": 7, "viewer_pid": 20,
        "owner_pid": 21, "viewer_birth": {"kind": "windows", "created": 100},
        "owner_birth": {"kind": "windows", "created": 100}, "title_prefix": "AgentKit #1 ",
        "interactive": False}
    value.update(changes)
    (root / ".ai/runtime").mkdir(parents=True, exist_ok=True)
    path = terminal_mirror.record_path(root, value["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value, path


def identities(monkeypatch):
    monkeypatch.setattr(terminal_mirror.process_identity, "fingerprint",
                        lambda pid: {"kind": "windows", "created": 100})
    monkeypatch.setattr(terminal_frame, "owner", lambda hwnd: 21)
    monkeypatch.setattr(terminal_frame, "title", lambda hwnd: "AgentKit #1 fixture")


def test_png_preserves_pixel_order_dimensions_and_color():
    encoded = terminal_frame.png(2, 2, bytes([0, 0, 255, 0, 0, 255, 0, 0,
                                            255, 0, 0, 0, 255, 255, 255, 0]))
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", encoded[16:24]) == (2, 2)
    offset, data = 8, b""
    while offset < len(encoded):
        length = int.from_bytes(encoded[offset:offset + 4], "big")
        kind = encoded[offset + 4:offset + 8]
        block = encoded[offset + 8:offset + 8 + length]
        assert zlib.crc32(kind + block) == int.from_bytes(encoded[offset + 8 + length:offset + 12 + length], "big")
        if kind == b"IDAT":
            data += block
        offset += length + 12
    assert zlib.decompress(data) == b"\0\xff\0\0\0\xff\0\0\0\0\xff\xff\xff\xff"


@pytest.mark.parametrize("width,height,pixels", [(0, 1, b""), (-1, -1, b"1234"),
    (2, 1, b"1234"), (terminal_frame.MAX_PIXELS + 1, 1, b"")])
def test_png_refuses_invalid_or_unbounded_frames(width, height, pixels):
    with pytest.raises(ValueError):
        terminal_frame.png(width, height, pixels)


@pytest.mark.parametrize("change", [{"viewer_birth": {"kind": "windows", "created": 99}},
    {"owner_birth": None}, {"viewer_pid": "20"}, {"hwnd": True}, {"title_prefix": ""},
    {"hwnd": 0}, {"key": "process-2"}])
def test_registration_refuses_pid_reuse_and_malformed_values(tmp_path, monkeypatch, change):
    identities(monkeypatch)
    _, path = record(tmp_path)
    value = json.loads(path.read_text())
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(PermissionError):
        terminal_mirror.verified(tmp_path, "process-1")


def test_current_hosting_window_and_tab_must_match(tmp_path, monkeypatch):
    identities(monkeypatch)
    record(tmp_path)
    assert terminal_mirror.verified(tmp_path, "process-1")["hwnd"] == 7
    monkeypatch.setattr(terminal_frame, "title", lambda hwnd: "Another terminal tab")
    with pytest.raises(PermissionError):
        terminal_mirror.verified(tmp_path, "process-1")
    monkeypatch.setattr(terminal_frame, "owner", lambda hwnd: 99)
    with pytest.raises(PermissionError):
        terminal_mirror.verified(tmp_path, "process-1")


@pytest.mark.parametrize("key", ["../.env", "manager-../secret", "process-0", "process-1/extra", "manager-"])
def test_registry_refuses_arbitrary_paths(tmp_path, key):
    with pytest.raises(ValueError):
        terminal_mirror.record_path(tmp_path, key)


def test_stopped_process_cannot_capture_other_live_window(project_root, conn, monkeypatch):
    identities(monkeypatch)
    record(project_root)
    conn.execute("INSERT INTO processes(id,purpose,provider,status,started_at,launch_json,child_launch_state) "
                 "VALUES(1,'worker','codex','FINISHED','2026-10-05','{}','NOT_STARTED')")
    conn.commit()
    with pytest.raises(PermissionError, match="stopped"):
        terminal_mirror.authorized(project_root, "process-1")
    conn.execute("UPDATE processes SET status='RUNNING' WHERE id=1")
    conn.commit()
    assert terminal_mirror.authorized(project_root, "process-1")["interactive"] is False


def test_manager_requires_fresh_exact_lease(project_root, conn, monkeypatch):
    from agentkit import manager
    identities(monkeypatch)
    record(project_root, key="manager-demo", interactive=True, session_ref="session-one")
    lease = {"pid": 20, "session_ref": "session-one"}
    monkeypatch.setattr(manager, "lease", lambda *args: lease)
    monkeypatch.setattr(manager, "fresh", lambda item: True)
    assert terminal_mirror.authorized(project_root, "manager-demo")["interactive"]
    lease["session_ref"] = "session-two"
    with pytest.raises(PermissionError, match="identity changed"):
        terminal_mirror.authorized(project_root, "manager-demo")
    lease["session_ref"] = "session-one"
    monkeypatch.setattr(manager, "fresh", lambda item: False)
    with pytest.raises(PermissionError, match="not active"):
        terminal_mirror.authorized(project_root, "manager-demo")


@pytest.mark.parametrize("text", ["", "x" * 4001, "line\nline", "\x1b[31m", None])
def test_manager_input_refuses_control_sequences_and_oversized_input(tmp_path, text):
    with pytest.raises(ValueError):
        terminal_manager.send(tmp_path, "demo", text)


def test_worker_readonly_registration_cannot_receive_keyboard(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_mirror, "authorized", lambda *args: {"interactive": False})
    with pytest.raises(PermissionError, match="read-only"):
        terminal_manager.send(tmp_path, "demo", "Continue")


@pytest.mark.parametrize("name,argv,provider,allowed", [
    ("codex.exe", "codex", "codex", True),
    ("codex.exe", "codex exec task", "codex", False),
    ("claude.exe", "claude --print task", "claude-code", False),
    ("claude.exe", 'claude "--print" task', "claude-code", False),
    ("codex.exe", 'codex "exec" task', "codex", False),
    ("claude.exe", "claude --output-format=json", "claude-code", False),
    ("claude.exe", "claude", "claude-code", True),
    ("powershell.exe", "powershell codex", "codex", False),
    ("Code.exe", "Code codex", "codex", False),
])
def test_only_actual_interactive_manager_cli_accepts_input(monkeypatch, name, argv, provider, allowed):
    reply = SimpleNamespace(returncode=0, stdout=json.dumps({"Name": name, "CommandLine": argv}))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: reply)
    assert terminal_manager.interactive_cli(20, provider) is allowed


def test_operator_token_and_exact_manager_payload_required(project_root, monkeypatch):
    delivered = []
    monkeypatch.setattr(terminal_manager, "send", lambda *args: delivered.append(args))
    body = json.dumps({"job_id": "demo", "text": "Continue the current task"}).encode()
    controls = dashboard_actions.Controls(project_root, enabled=True)
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    assert controls.post("/api/terminal/input", headers, io.BytesIO(body))[0] == 403
    headers["X-AgentKit-Token"] = controls.token
    assert controls.post("/api/terminal/input", headers, io.BytesIO(body))[0] == 200
    assert delivered == [(project_root, "demo", "Continue the current task")]
    readonly = dashboard_actions.Controls(project_root)
    assert readonly.post("/api/terminal/input", headers, io.BytesIO(body))[0] == 405
    body = json.dumps({"job_id": "demo", "text": "message", "pid": 99}).encode()
    headers["Content-Length"] = str(len(body))
    assert controls.post("/api/terminal/input", headers, io.BytesIO(body))[0] == 409
    assert len(delivered) == 1


def test_terminal_http_serves_image_and_refuses_remote_origin(project_root, monkeypatch):
    image = terminal_frame.png(1, 1, b"\x00\x00\xff\x00")
    monkeypatch.setattr(terminal_mirror, "frame", lambda *args: (200, image, "image/png"))
    server = dashboard.make_server(project_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/api/terminal/process-1")
        response = client.getresponse()
        assert response.status == 200 and response.read() == image
        assert response.getheader("Content-Type") == "image/png"
        assert "img-src 'self' blob:" in response.getheader("Content-Security-Policy")
        client.close()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/api/terminal/process-1", headers={"Origin": "https://remote.example"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
