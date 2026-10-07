"""Transport framing and authority survive corrupt peers without any model calls."""
import io
import json

import pytest

from agentkit.wsl_dispatch import windows_path
from agentkit.wsl_runtime import WslConfig, child_environment
from agentkit.wsl_transport import (
    TO_HOST,
    TO_WORKER,
    Codec,
    ProtocolError,
    accept_hello,
    hello_frame,
)


def test_frame_authentication_direction_and_replay():
    key = b"x" * 32
    host, worker = Codec(key, sends=TO_WORKER), Codec(key, sends=TO_HOST)
    data = host.frame("probe", {}, "request")
    assert worker.decode(data)["id"] == "request"
    with pytest.raises(ProtocolError):
        worker.decode(data)
    with pytest.raises(ProtocolError):
        host.decode(data)
    with pytest.raises(ProtocolError):
        Codec(b"y" * 32, sends=TO_HOST).decode(data)


def test_hello_and_session_frames_share_ordered_identity():
    worker, launch = accept_hello(hello_frame(b"x" * 32, {"argv": ["/usr/bin/python3"], "cwd": "/tmp"}))
    assert launch["argv"] == ["/usr/bin/python3"]
    assert "key" not in launch
    host = Codec(b"x" * 32, sends=TO_WORKER)
    buffer = io.BytesIO()
    host.send(buffer, "probe", {}, "first")
    assert worker.decode(buffer.getvalue())["seq"] == 1


@pytest.mark.parametrize("value", ["/mnt/c/claude.exe", "C:/claude.exe", "/usr/../bin/claude", "/usr/bin/claude.exe"])
def test_linux_worker_refuses_windows_and_ambiguous_executables(value):
    with pytest.raises(ValueError):
        WslConfig("Ubuntu", "agentkit", "/usr/bin/python3", value)


def test_credential_and_authority_variables_never_reach_linux():
    env = child_environment({"HOME": "/home/worker", "PATH": "/mnt/c/bin:/usr/bin", "AGENTKIT_ROOT": "C:/repo",
                             "ANTHROPIC_API_KEY": "secret", "AWS_ACCESS_KEY_ID": "secret"}, {}, "/tmp/private/s")
    assert "AGENTKIT_ROOT" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["AGENTKIT_WSL_SOCKET"] == "/tmp/private/s"


@pytest.mark.parametrize("value", ["/home/user/file", "//server/path", "C:/other", "../opaque\\path"])
def test_host_path_translation_refuses_other_namespaces(value):
    with pytest.raises(ValueError):
        windows_path(value)


def test_host_path_translation_keeps_drive_and_relative_paths():
    assert windows_path("/mnt/e/project/file.py") == "E:/project/file.py"
    assert windows_path("services/file.py") == "services/file.py"


def test_unconfirmed_linux_exit_preserves_worker_ownership(conn, project_root, monkeypatch):
    from agentkit import db, processes, runner, transport_ownership
    task_id = db.create_task(conn, title="unconfirmed WSL", status="RUNNING", generation=1,
                             worktree=str(project_root), owned_paths=["services/retry.py"])
    identifier = conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at) VALUES(?,?,?,?,?,?)",
        ("worker", "claude-code", task_id, 1, json.dumps({"env": {"AGENTKIT_TRANSPORT": "wsl"}}), db.utcnow())).lastrowid
    process = processes.get(conn, identifier)
    runner.finish_worker(conn, project_root, process, 1, "relay disappeared")
    current = processes.get(conn, identifier)
    assert current["child_launch_state"] == "WSL_UNCONFIRMED"
    assert processes.ownership_uncertain(current)
    assert db.get_task(conn, task_id)["status"] == "RUNNING"
    assert db.get_task(conn, task_id)["attempts"] == 0
    assert not transport_ownership.exit_confirmed(project_root, identifier)


def test_recovery_does_not_release_live_orphan_group(project_root, monkeypatch):
    from agentkit import transport_ownership, wsl_host
    target = transport_ownership.path(project_root, 7)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"stopped": False, "identity": {}, "config": {}}))
    monkeypatch.setattr(wsl_host, "confirmed_stopped", lambda root, identifier: True)
    monkeypatch.setattr(transport_ownership, "group_absent", lambda data: False)
    assert not transport_ownership.recover_exit(project_root, 7)
    assert not transport_ownership.exit_confirmed(project_root, 7)
    monkeypatch.setattr(transport_ownership, "group_absent", lambda data: True)
    assert transport_ownership.recover_exit(project_root, 7)
    assert transport_ownership.exit_confirmed(project_root, 7)
