"""Opt-in actual CLI manager keyboard; workers and native editor chats cannot receive it."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from ctypes import wintypes as w
from pathlib import Path

from . import db, manager, terminal_frame, terminal_mirror


def interactive_cli(pid: int, provider: str) -> bool:
    """Reject shells, editor bridges and print/exec-mode CLI sessions."""
    if type(pid) is not int or pid <= 0 or provider not in ("codex", "claude-code"):
        return False
    command = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction Stop | "
               "Select-Object Name,CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        process = json.loads(result.stdout)
        argv = process.get("CommandLine", "")
        name = process.get("Name", "").casefold()
        if result.returncode or re.search(r'(?:^|[\s"])(?:exec|--print|-p|--output-format|--json)(?=[\s"=]|$)', argv, re.I):
            return False
        if provider == "codex":
            return name == "codex.exe" or (name == "node.exe" and "codex" in argv.casefold())
        return name == "claude.exe" or (name == "node.exe" and "claude-code" in argv.casefold())
    except (ValueError, TypeError, AttributeError):
        return False


def attach(pid: int):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [w.DWORD]
    kernel.FreeConsole()  # Only this isolated helper's inherited console.
    if not kernel.AttachConsole(pid):
        raise OSError("Cannot attach to the selected CLI manager console.")
    return kernel


def register(root: Path, job_id: str, pid: int) -> None:
    if os.name != "nt":
        raise OSError("Manager window keyboard currently requires Windows.")
    conn = db.connect_readonly(root)
    try:
        lease = manager.lease(conn, job_id)
        if not lease or not manager.fresh(lease) or lease["pid"] != pid:
            raise PermissionError("Registration requires the current fresh manager lease and exact CLI PID.")
        if not interactive_cli(pid, lease["provider"]):
            raise PermissionError("Only an interactive Claude/Codex CLI manager can be registered; editor bridges and print-mode sessions are excluded.")
        key = "manager-" + job_id
        terminal_mirror.record_path(root, key)  # Validate the key before changing a title.
        attach(pid)
        marker = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:10]
        expected = f"AgentKit manager [{marker}] {job_id} "
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetConsoleTitleW.argtypes = [w.LPCWSTR]
        kernel.SetConsoleTitleW(expected + lease["provider"])
        hwnd = terminal_frame.console_window(expected)
        if not hwnd or not terminal_mirror.bind(root, key, hwnd, pid, expected,
                interactive=True, job_id=job_id, session_ref=lease["session_ref"]):
            raise OSError("This terminal host has no capturable manager window.")
    finally:
        conn.close()


def send(root: Path, job_id: str, text: str) -> None:
    if not isinstance(job_id, str) or not isinstance(text, str) or not 0 < len(text) <= 4000:
        raise ValueError("Manager input needs a job ID and 1-4000 characters.")
    if any(not char.isprintable() for char in text):
        raise ValueError("Send one text line; terminal control sequences are refused.")
    key = "manager-" + job_id
    record = terminal_mirror.authorized(root, key)
    if record.get("interactive") is not True:
        raise PermissionError("This terminal is read-only.")
    conn = db.connect_readonly(root)
    try:
        lease = manager.lease(conn, job_id)
        if not lease or not interactive_cli(record["viewer_pid"], lease["provider"]):
            raise PermissionError("The manager CLI is not accepting interactive input.")
    finally:
        conn.close()
    try:
        reply = subprocess.run([sys.executable, "-m", "agentkit.terminal_manager", "input", str(root), job_id],
                               input=text.encode("utf-8"), capture_output=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Manager input outcome is unknown; do not resend automatically.") from exc
    if reply.returncode:
        raise OSError("Manager input was not confirmed; do not assume the agent received it.")


def write_keys(root: Path, job_id: str, text: str) -> None:
    record = terminal_mirror.authorized(root, "manager-" + job_id)
    if not record.get("interactive") or any(not c.isprintable() for c in text) or not 0 < len(text) <= 4000:
        raise PermissionError("Invalid or read-only manager terminal input.")
    conn = db.connect_readonly(root)
    try:
        lease = manager.lease(conn, job_id)
        if not lease or not interactive_cli(record["viewer_pid"], lease["provider"]):
            raise PermissionError("Manager ownership or interactive CLI changed.")
    finally:
        conn.close()
    kernel = attach(record["viewer_pid"])
    terminal_mirror.authorized(root, "manager-" + job_id)
    _write_input(kernel, text)


def _write_input(kernel, text: str) -> None:
    """Native console primitive; production calls must first pass manager guards."""
    if not 0 < len(text) <= 4000 or any(not c.isprintable() for c in text):
        raise ValueError("Send one printable text line only.")
    class Key(ctypes.Structure):
        _fields_ = [("down", w.BOOL), ("repeat", w.WORD), ("virtual", w.WORD),
                    ("scan", w.WORD), ("char", w.WCHAR), ("control", w.DWORD)]
    class Payload(ctypes.Union):
        _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]
    class Input(ctypes.Structure):
        _fields_ = [("event", w.WORD), ("payload", Payload)]
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    handle = kernel.CreateFileW("CONIN$", 0x40000000, 3, None, 3, 0, None)
    if handle == w.HANDLE(-1).value:
        raise OSError("Manager input buffer unavailable.")
    try:
        units = text.encode("utf-16-le", errors="strict")
        characters = [chr(int.from_bytes(units[i:i + 2], "little")) for i in range(0, len(units), 2)] + ["\r"]
        events = (Input * (len(characters) * 2))()
        for i, char in enumerate(characters):
            for down in (0, 1):
                entry = events[2 * i + down]
                entry.event = 1
                entry.payload.key = Key(not down, 1, 13 if char == "\r" else 0, 0, char, 0)
        written = w.DWORD()
        kernel.WriteConsoleInputW.argtypes = [w.HANDLE, ctypes.POINTER(Input), w.DWORD, ctypes.POINTER(w.DWORD)]
        if not kernel.WriteConsoleInputW(handle, events, len(events), ctypes.byref(written)) or written.value != len(events):
            raise OSError("Manager input was not fully delivered.")
    finally:
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle(handle)


def command(args):
    root = Path(args.path or Path.cwd()).resolve()
    try:
        reply = subprocess.run([sys.executable, "-m", "agentkit.terminal_manager", "register", str(root), args.job, str(args.pid)],
                               timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Manager terminal registration timed out; check its state before retrying.") from exc
    return reply.returncode


def configure(sub):
    parser = sub.add_parser("terminal", help="register an existing interactive CLI manager; creates no agent")
    actions = parser.add_subparsers(dest="terminal_action", required=True)
    registration = actions.add_parser("register-manager")
    registration.add_argument("--job", required=True)
    registration.add_argument("--pid", type=int, required=True)
    registration.set_defaults(func=command)


def main():
    try:
        action, root, job = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
        if action == "register":
            register(root, job, int(sys.argv[4]))
        else:
            write_keys(root, job, sys.stdin.buffer.read(16_001).decode("utf-8"))
        return 0
    except (OSError, ValueError, PermissionError, sqlite3.Error, subprocess.TimeoutExpired):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
