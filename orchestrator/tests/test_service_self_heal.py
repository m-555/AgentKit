"""F30: the guardian survives an editor restart, and a lost guardian is replaced.

`agentkit service start` ran from the manager chat's shell, so the guardian
lived in VS Code's process tree. VS Code restarted, the guardian died, and the
supervisor ran on with nobody to restart it after a crash. On Windows the
guardian is now created by WMI, outside any caller's tree, and the supervisor
starts a replacement guardian that adopts it instead of starting a second.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agentkit import service, service_guardian, service_launch
from agentkit.reconcile import pid_alive


def _health(root, **values):
    path = root / ".ai/runtime/supervisor-health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_lost_guardian_is_replaced_by_one_that_adopts_the_running_supervisor(project_root):
    _health(project_root, guardian_pid=_dead_pid(), supervisor_pid=os.getpid(), status="running")
    launches = []
    note = service_launch.heal(project_root, "config",
                               launch=lambda argv, **k: launches.append((argv, k)) or SimpleNamespace(pid=4321))
    argv, options = launches[0]
    assert argv[-4:] == [str(project_root.resolve()), "config", "--adopt", str(os.getpid())]
    assert "4321" in note and (project_root / ".ai/runtime/supervisor.pid").read_text() == "4321"
    if os.name == "nt":  # its own console, apart from the caller's, with no window (F34)
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert not options["creationflags"] & subprocess.DETACHED_PROCESS


@pytest.mark.parametrize("status", ["running", "stopped"])
def test_a_live_or_deliberately_stopped_guardian_is_left_alone(project_root, status):
    guardian = os.getpid() if status == "running" else _dead_pid()
    _health(project_root, guardian_pid=guardian, supervisor_pid=os.getpid(), status=status)
    assert service_launch.heal(project_root, "config", launch=lambda *a, **k: pytest.fail("second guardian")) is None


def test_the_adopting_guardian_waits_for_that_supervisor_then_supervises(project_root):
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sleeps, launches = [], []

    def sleeper(seconds):
        sleeps.append(seconds)
        if running.poll() is None:
            running.kill()
            running.wait()

    def spawn(argv, **kwargs):
        launches.append(argv)
        return SimpleNamespace(pid=99, wait=lambda: 0)

    service_guardian.run(project_root, "config", spawn=spawn, sleeper=sleeper, max_iterations=1, adopt=running.pid)
    assert launches == [launches[0]] and launches[0][-1] == "--worker"  # no second supervisor before it exited
    assert sleeps[0] == 5 and sleeps[1] == 20


def test_start_on_windows_creates_the_guardian_through_wmi(monkeypatch):
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service_launch, "wmi_create", lambda argv, cwd: 8765)
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: pytest.fail("inside the caller's tree"))
    assert service._popen(["python", "-m", "agentkit.service"], cwd=".").pid == 8765


@pytest.mark.skipif(os.name != "nt", reason="WMI is Windows only")
def test_wmi_starts_a_process_outside_this_process_tree(tmp_path):
    pid = service_launch.wmi_create([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    assert pid and pid_alive(pid)
    try:
        query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"
        parent = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query],
                                capture_output=True, text=True, timeout=30).stdout.strip()
        assert parent and int(parent) != os.getpid()
    finally:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def test_wmi_gives_a_background_process_a_console_without_a_window():
    """F34: five console windows sat on the user's desktop, one per service and view.

    DETACHED_PROCESS left the uv tool's python.exe with no console. That file is a
    shim that starts the real interpreter as a console child, and a console child
    of a process without a console is given a new, visible one.
    """
    assert service_launch.WMI_FLAGS & 0x08000000  # CREATE_NO_WINDOW
    assert service_launch.WMI_FLAGS & 0x200  # CREATE_NEW_PROCESS_GROUP: a Ctrl+C elsewhere never reaches it
    assert not service_launch.WMI_FLAGS & 0x8  # DETACHED_PROCESS would make Windows ignore CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="console windows are Windows only")
def test_a_console_child_of_a_wmi_started_process_opens_no_window(tmp_path):
    report = tmp_path / "visible.txt"
    child = ("import ctypes; w = ctypes.windll.kernel32.GetConsoleWindow(); "
             f"open({str(report)!r}, 'w').write(str(bool(w and ctypes.windll.user32.IsWindowVisible(w))))")
    shim = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    pid = service_launch.wmi_create([sys.executable, "-c", shim], tmp_path)
    assert pid
    try:
        for _ in range(150):
            if report.exists() and report.read_text():
                break
            time.sleep(0.2)
        assert report.read_text() == "False"
    finally:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
