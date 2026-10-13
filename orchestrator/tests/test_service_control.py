"""F6: the supervisor's real state, and commands to start, stop, reload and autostart it.

After a pilot project's host rebooted on 2026-10-09, supervisor-health.json still said
"retrying" twelve hours after its guardian had died, nothing restarted it, and
`supervisor.pid` named the dead guardian while a newer one (started by hand)
ran without a pidfile, so a plain start would have launched a second
supervisor. Reloading code needed a one-off script and waited out the
guardian's crash backoff.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentkit import service, service_control, service_guardian

SLEEP = "import time; time.sleep(120)"


def _fake(root, *, worker=False, service_args=True):
    args = [sys.executable, "-c", SLEEP]
    if service_args:
        args += ["-m", "agentkit.service", str(root), "config"] + (["--worker"] if worker else [])
    return subprocess.Popen(args)


def _health(root, **fields):
    runtime = root / ".ai/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "supervisor-health.json").write_text(json.dumps({"at": "2026-10-09T07:00:37+00:00", **fields}))


@pytest.fixture
def live(project_root):
    guardian, child = _fake(project_root), _fake(project_root, worker=True)
    _health(project_root, guardian_pid=guardian.pid, supervisor_pid=child.pid, status="running")
    yield guardian, child
    for process in (guardian, child):
        process.kill()
        process.wait()


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_a_dead_guardian_is_reported_stopped_not_retrying(project_root):
    _health(project_root, guardian_pid=_dead_pid(), supervisor_pid=_dead_pid(), status="retrying")
    state = service_control.status(project_root)
    assert state["state"] == "stopped" and state["stale"] is True
    assert "agentkit service start" in state["note"]


def test_a_live_verified_guardian_is_running(project_root, live):
    state = service_control.status(project_root)
    assert (state["state"], state["stale"], state["guardian_alive"], state["supervisor_alive"]) == (
        "running", False, True, True)


def test_a_reused_pid_is_not_the_guardian(project_root):
    stranger = _fake(project_root, service_args=False)
    try:
        _health(project_root, guardian_pid=stranger.pid, status="running")
        assert service_control.status(project_root)["state"] == "stopped"
    finally:
        stranger.kill()
        stranger.wait()


def test_start_refuses_while_a_verified_guardian_runs_without_a_pidfile(project_root, live, monkeypatch):
    (project_root / ".ai/runtime/supervisor.pid").write_text(str(_dead_pid()))
    monkeypatch.setattr(service, "_popen", lambda *a, **k: pytest.fail("second supervisor started"))
    assert "already running" in service.start(project_root)


def test_start_launches_a_detached_guardian_and_records_it(project_root, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_popen", lambda argv, **k: calls.append((argv, k)) or SimpleNamespace(pid=4242))
    assert "started" in service.start(project_root)
    assert (project_root / ".ai/runtime/supervisor.pid").read_text() == "4242"
    argv, kwargs = calls[0]
    assert argv[-2:] == [str(project_root.resolve()), "config"]
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW  # a console, but no window (F34)
        assert not kwargs["creationflags"] & subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP


def test_stop_ends_guardian_then_supervisor_and_records_it(project_root, live):
    guardian, child = live
    result = service_control.stop(project_root)
    assert guardian.wait(timeout=10) is not None and child.wait(timeout=10) is not None
    assert result["stopped"] == [guardian.pid, child.pid]
    health = json.loads((project_root / ".ai/runtime/supervisor-health.json").read_text())
    assert health["status"] == "stopped" and service_control.status(project_root)["stale"] is False


def test_stop_never_kills_an_unverified_process(project_root):
    stranger = _fake(project_root, service_args=False)
    try:
        _health(project_root, guardian_pid=stranger.pid, supervisor_pid=stranger.pid, status="running")
        assert service_control.stop(project_root)["stopped"] == []
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_reload_restarts_only_the_supervisor_child(project_root, live):
    guardian, child = live
    assert service_control.reload(project_root)["reloaded"] == child.pid
    assert child.wait(timeout=10) is not None and guardian.poll() is None
    assert (project_root / ".ai/runtime" / service_control.RELOAD_MARKER).exists()


def test_the_guardian_restarts_at_once_after_a_reload(project_root):
    marker = project_root / ".ai/runtime" / service_control.RELOAD_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("reload")
    delays = []
    service_guardian.run(project_root, "config", spawn=lambda *a, **k: SimpleNamespace(pid=7, wait=lambda: 1),
                         sleeper=delays.append, max_iterations=2)
    assert delays == [1, 40]  # the reload is not a crash; the next exit is
    assert not marker.exists()


def test_autostart_installs_and_removes_one_startup_entry(project_root, tmp_path):
    folder = tmp_path / "Startup"
    if os.name != "nt":
        with pytest.raises(NotImplementedError):
            service_control.install_autostart(project_root, folder=folder)
        return
    entry = service_control.install_autostart(project_root, folder=folder)
    assert entry.parent == folder and entry.suffix == ".cmd"
    text = entry.read_text(encoding="utf-8")
    assert str(project_root.resolve()) in text and "service" in text and "start" in text
    assert service_control.install_autostart(project_root, folder=folder) == entry  # idempotent
    assert len(list(folder.iterdir())) == 1
    assert service_control.remove_autostart(project_root, folder=folder) is True and not entry.exists()
