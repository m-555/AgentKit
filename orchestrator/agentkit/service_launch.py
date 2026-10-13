"""Keep the guardian alive through editor restarts (F30).

`agentkit service start` ran from the manager chat's shell, so the guardian
lived in the editor's process tree: when VS Code restarted, the guardian died
and nothing could restart a crashed supervisor. On Windows the guardian is now
created by WMI, outside the caller's process tree and job, with a console of its
own, so neither closing the editor nor a console Ctrl+C reaches it. And the supervisor
checks its guardian after every pass: when it is gone, it starts a new one that
adopts the running supervisor instead of starting a second.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .locking import exclusive
from .reconcile import pid_alive

#: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: a console apart from the caller's, without a
#: window (F34). DETACHED_PROCESS (no console at all) opened a visible window per service and
#: view: the uv tool's python.exe is a shim that starts the real interpreter as a console
#: child, and a console child of a process without a console is given a new, visible one.
WINDOWLESS = 0x08000000 | 0x200
#: Win32_ProcessStartup.CreateFlags.
WMI_FLAGS = WINDOWLESS


def windowless_flags() -> int:
    """Popen creationflags for a long-lived background process: WINDOWLESS on Windows only."""
    return WINDOWLESS if os.name == "nt" else 0
_SCRIPT = ("$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
           "-Property @{CreateFlags=[uint32]$env:AGENTKIT_WMI_FLAGS; ShowWindow=[uint16]0}; "
           "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
           "@{CommandLine=$env:AGENTKIT_WMI_COMMAND; CurrentDirectory=$env:AGENTKIT_WMI_CWD; "
           "ProcessStartupInformation=$s}; Write-Output \"$($r.ReturnValue) $($r.ProcessId)\"")


def wmi_create(argv: list[str], cwd) -> int | None:
    """Start argv through WMI; its PID, or None when WMI refused (the caller falls back)."""
    env = {**os.environ, "AGENTKIT_WMI_COMMAND": subprocess.list2cmdline(argv),
           "AGENTKIT_WMI_CWD": str(cwd), "AGENTKIT_WMI_FLAGS": str(WMI_FLAGS)}
    try:
        reply = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                               env=env, capture_output=True, text=True, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = reply.stdout.split()
    if reply.returncode or len(parts) != 2 or parts[0] != "0" or not parts[1].isdecimal():
        return None
    return int(parts[1])


def heal(root, cap: str, *, launch=None) -> str | None:
    """Called by the supervisor after each pass: restart a guardian that died."""
    from . import service, service_control
    health = service_control._health(root)
    guardian, supervisor = health.get("guardian_pid"), health.get("supervisor_pid")
    if health.get("status") == "stopped" or (isinstance(guardian, int) and pid_alive(guardian)):
        return None  # Cheap check every pass; a stopped service stays stopped.
    try:
        with exclusive(root, "service-start", timeout=0):
            if service_control.status(root)["guardian_alive"]:
                return None
            adopt = supervisor if isinstance(supervisor, int) else os.getpid()
            argv = [sys.executable, "-m", "agentkit.service", str(Path(root).resolve()), cap, "--adopt", str(adopt)]
            process = (launch or service._popen)(argv, cwd=Path(__file__).resolve().parents[1],
                                                 stdin=subprocess.DEVNULL, creationflags=windowless_flags())
            (Path(root) / ".ai" / "runtime" / "supervisor.pid").write_text(str(process.pid))
            return f"guardian {guardian} was gone; started guardian {process.pid} adopting supervisor {adopt}"
    except TimeoutError:
        return None  # A start is in progress.
