"""A reused PID is a different process; unreadable identity remains conservative."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path


def fingerprint(pid):
    if not pid:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return windows_birth(pid) if ctypes.get_last_error() == 5 else None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    return None
                created = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime
                born = datetime.fromtimestamp((created - 116444736000000000) / 10000000, UTC)
                return {"kind": "windows", "created": created, "born": born.isoformat()}
            finally:
                kernel.CloseHandle(handle)
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"kind": "linux", "created": parts[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, AttributeError, ValueError, IndexError):
        return None


def alive(record, field="pid", *, probe=None):
    from .reconcile import pid_alive
    pid = record.get(field)
    if not pid or not (probe or pid_alive)(pid):
        return False
    current = fingerprint(pid)
    saved = record.get(field + "_identity")
    if saved and current:
        try:
            previous = json.loads(saved)
            if current.get("source") == "cim" and previous.get("kind") == "windows":
                return abs(previous["created"] - current["created"]) < 10
            return previous == current
        except (ValueError, TypeError, KeyError, AttributeError):
            return True
    # Legacy terminal records have no birth token. A new Windows process born
    # after the recorded exit cannot be the old owner. Do not kill that process.
    ended = record.get("ended_at")
    if current and current.get("born") and ended and record.get("status") not in ("STARTING", "RUNNING"):
        try:
            return datetime.fromisoformat(current["born"]) <= datetime.fromisoformat(ended) + timedelta(seconds=2)
        except (ValueError, TypeError):
            pass
    return True


def windows_birth(pid):
    """Protected reused PIDs can still expose birth time through read-only CIM.

    CIM rounds to microseconds; allow less than one microsecond when comparing
    its FILETIME to a stored GetProcessTimes token. Unknown remains conservative.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    command = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction Stop | "
               "ForEach-Object { $_.CreationDate.ToFileTimeUtc() }")
    try:
        reply = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if reply.returncode:
            return None
        created = int(reply.stdout.strip())
        if created <= 116444736000000000:
            return None
        born = datetime.fromtimestamp((created - 116444736000000000) / 10000000, UTC)
        return {"kind": "windows", "created": created, "born": born.isoformat(), "source": "cim"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
