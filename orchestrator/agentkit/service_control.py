"""What the background supervisor is really doing, and the commands that change it (F6).

The guardian writes supervisor-health.json, but a file cannot report its own
writer's death: after a reboot it kept saying "retrying" for twelve hours.
Status therefore checks that the recorded guardian and supervisor are alive
and are this project's service processes; a reused PID is not. Stop and reload
act only on processes that pass that check, so they never kill a stranger.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .locking import atomic_write, exclusive
from .reconcile import pid_alive
from .service_guardian import RELOAD_MARKER

__all__ = ["RELOAD_MARKER", "command_line", "install_autostart", "is_service", "reload",
           "remove_autostart", "status", "stop"]
HEALTH = "supervisor-health.json"


def _runtime(root) -> Path:
    return Path(root).resolve() / ".ai" / "runtime"


def _health(root) -> dict:
    try:
        data = json.loads((_runtime(root) / HEALTH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def command_line(pid) -> str | None:
    """The process's command line, or None when it cannot be read."""
    if os.name == "nt":
        query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' -ErrorAction Stop).CommandLine"
        try:
            reply = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                                   capture_output=True, text=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return reply.stdout.strip() or None if reply.returncode == 0 else None
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip() or None
    except OSError:
        return None


def _norm(text: str) -> str:
    return text.replace("\\", "/").casefold()


def is_service(pid, root, *, worker: bool) -> bool:
    """A live process running this project's guardian (worker=False) or supervisor (worker=True)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
        return False
    line = command_line(pid)
    if not line:
        return False
    line = _norm(line)
    return ("agentkit.service" in line and _norm(str(Path(root).resolve())) in line
            and ("--worker" in line) == worker)


def status(root) -> dict:
    health = _health(root)
    guardian_pid, supervisor_pid = health.get("guardian_pid"), health.get("supervisor_pid")
    guardian = is_service(guardian_pid, root, worker=False)
    supervisor = is_service(supervisor_pid, root, worker=True)
    recorded = health.get("status")
    if guardian:
        state, note = recorded or "starting", ""
        if recorded in ("retrying", "idle"):
            note = f"Next supervisor start in about {health.get('restart_in_seconds')} s."
    elif supervisor:
        state, note = "orphaned", "The supervisor runs without its guardian; run `agentkit service stop`, then start."
    else:
        state, note = "stopped", "Start it with `agentkit service start`."
    stale = not guardian and recorded not in (None, "stopped")
    if stale:
        note = (f"Health says {recorded!r} (written {health.get('at')}), but guardian {guardian_pid} "
                f"is not running: the host restarted or it crashed. {note}")
    return {"state": state, "stale": stale, "recorded_status": recorded, "health_at": health.get("at"),
            "guardian_pid": guardian_pid, "guardian_alive": guardian,
            "supervisor_pid": supervisor_pid, "supervisor_alive": supervisor, "note": note}


def _terminate(pid, timeout: float = 15) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)


def stop(root) -> dict:
    """Stop the verified guardian first, so it cannot restart the supervisor, then the supervisor.

    Worker monitors are separate processes and keep running; nothing new launches.
    """
    health = _health(root)
    stopped = []
    for field, worker in (("guardian_pid", False), ("supervisor_pid", True)):
        pid = health.get(field)
        if is_service(pid, root, worker=worker):
            _terminate(pid)
            stopped.append(pid)
    atomic_write(_runtime(root) / HEALTH, json.dumps(
        {**health, "status": "stopped", "at": db.utcnow(), "stopped_by": "agentkit service stop"}) + "\n")
    (_runtime(root) / "supervisor.pid").unlink(missing_ok=True)
    return {"stopped": stopped, "note": "" if stopped else "No verified supervisor process was running."}


def reload(root) -> dict:
    """Restart only the supervisor so it loads current code; the guardian starts it again at once."""
    health = _health(root)
    guardian, child = health.get("guardian_pid"), health.get("supervisor_pid")
    if not is_service(guardian, root, worker=False):
        raise RuntimeError("No verified guardian is running; use `agentkit service start`.")
    if not is_service(child, root, worker=True):
        raise RuntimeError("The guardian has no running supervisor to reload; it will start one itself.")
    # No pass is cut mid-way: wait for the supervisor, scheduler and integration locks.
    with exclusive(root, "supervisor", timeout=120), exclusive(root, "scheduler", timeout=60), \
            exclusive(root, "integration", timeout=60):
        atomic_write(_runtime(root) / RELOAD_MARKER, db.utcnow() + "\n")
        _terminate(child)
    return {"reloaded": child, "guardian": guardian}


def _startup_folder() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def _entry(root, folder) -> Path:
    resolved = str(Path(root).resolve())
    digest = hashlib.sha256(_norm(resolved).encode()).hexdigest()[:8]
    return Path(folder or _startup_folder()) / f"AgentKit supervisor - {Path(resolved).name}-{digest}.cmd"


def install_autostart(root, folder=None) -> Path:
    """Start this project's supervisor at logon. Start refuses a second supervisor, so it is idempotent."""
    if os.name != "nt":
        raise NotImplementedError("autostart is implemented for Windows; elsewhere run "
                                  "`agentkit service start` from a user service unit")
    entry = _entry(root, folder)
    entry.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    resolved = str(Path(root).resolve())
    call = f"from agentkit.cli import main; main(['--path', r'{resolved}', 'service', 'start'])"
    entry.write_text("@echo off\r\n"
                     f"rem AgentKit: start the supervisor for {resolved} at logon (no second copy).\r\n"
                     f'start "" "{windowless if windowless.exists() else python}" -c "{call}"\r\n',
                     encoding="utf-8")
    return entry


def remove_autostart(root, folder=None) -> bool:
    entry = _entry(root, folder)
    if not entry.exists():
        return False
    entry.unlink()
    return True
