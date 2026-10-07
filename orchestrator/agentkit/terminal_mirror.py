"""Bind exact CLI windows to project sessions; bounded helpers capture their pixels."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import db, process_identity, terminal_frame
from .locking import atomic_write


def prefix(root: Path, identifier: int) -> str:
    key = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:10]
    return f"AgentKit #{identifier} [{key}] "


def record_path(root: Path, key: str) -> Path:
    if not re.fullmatch(r"(?:process-[1-9][0-9]{0,8}|manager-[A-Za-z0-9_.-]{1,100})", key):
        raise ValueError("Invalid terminal session key.")
    project = root.resolve(strict=True)
    runtime = (project / ".ai" / "runtime").resolve(strict=True)
    runtime.relative_to(project)
    path = runtime / "terminal-mirrors" / (key + ".json")
    path.resolve().relative_to(runtime)
    return path


def bind(root: Path, key: str, hwnd: int, viewer_pid: int, expected_title: str, **metadata) -> bool:
    try:
        owner_pid = terminal_frame.owner(hwnd)
        viewer_birth = process_identity.fingerprint(viewer_pid)
        owner_birth = process_identity.fingerprint(owner_pid)
        if not viewer_birth or not owner_birth or not terminal_frame.title(hwnd).startswith(expected_title):
            return False
        record = {"version": 1, "key": key, "hwnd": hwnd, "viewer_pid": viewer_pid,
                  "viewer_birth": viewer_birth, "owner_pid": owner_pid,
                  "owner_birth": owner_birth, "title_prefix": expected_title, **metadata}
        path = record_path(root, key)
        if path.exists() and path.read_text(encoding="utf-8") == json.dumps(record) + "\n":
            return True
        atomic_write(path, json.dumps(record) + "\n")
        return True
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False


def register_viewer(root: Path, identifier: int) -> bool:
    if os.name != "nt":
        return False
    try:
        expected = prefix(root, identifier)
        hwnd = terminal_frame.console_window(expected)
        return bool(hwnd and bind(root, f"process-{identifier}", hwnd, os.getpid(), expected,
                                  interactive=False, process_id=identifier))
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False


def verified(root: Path, key: str) -> dict:
    path = record_path(root, key)
    if path.stat().st_size > 8192:
        raise PermissionError("Terminal registration is invalid.")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("key") != key:
        raise PermissionError("Terminal registration is invalid.")
    if (type(record.get("hwnd")) is not int or record["hwnd"] <= 0 or
            not isinstance(record.get("title_prefix"), str) or not record["title_prefix"]):
        raise PermissionError("Terminal window registration is invalid.")
    for name in ("viewer", "owner"):
        expected = record.get(name + "_birth")
        pid = record.get(name + "_pid")
        if type(pid) is not int or pid <= 0 or not isinstance(expected, dict):
            raise PermissionError("Terminal process registration is invalid.")
        actual = process_identity.fingerprint(pid)
        if not expected or not actual or (expected.get("kind"), expected.get("created")) != (actual.get("kind"), actual.get("created")):
            raise PermissionError("Terminal ownership changed or stopped.")
    if terminal_frame.owner(record["hwnd"]) != record["owner_pid"]:
        raise PermissionError("Terminal window identity changed.")
    if not terminal_frame.title(record["hwnd"]).startswith(record["title_prefix"]):
        raise PermissionError("The terminal is showing a different tab or session.")
    return record


def authorized(root: Path, key: str) -> dict:
    """Only active recorded CLI processes, or the exact fresh external CLI manager."""
    conn = db.connect_readonly(root)
    try:
        record = verified(root, key)
        if key.startswith("process-"):
            identifier = int(key.removeprefix("process-"))
            process = conn.execute("SELECT * FROM processes WHERE id=?", (identifier,)).fetchone()
            if not process:
                raise PermissionError("CLI process is not recorded.")
            from .processes import owning
            if identifier not in {item["id"] for item in owning(conn)}:
                raise PermissionError("CLI process is stopped.")
        else:
            from . import manager
            lease = manager.lease(conn, key.removeprefix("manager-"))
            if not lease or not manager.fresh(lease) or lease.get("released_at"):
                raise PermissionError("CLI manager lease is not active.")
            if lease["pid"] != record["viewer_pid"] or lease.get("session_ref") != record.get("session_ref"):
                raise PermissionError("CLI manager identity changed.")
        return record
    finally:
        conn.close()


def frame(root: Path, key: str) -> tuple[int, bytes, str]:
    try:
        authorized(root, key)
        # PrintWindow is synchronous. A helper bounds hangs without blocking the
        # dashboard, changing desktop focus or capturing another application's pixels.
        reply = subprocess.run([sys.executable, "-m", "agentkit.terminal_mirror", str(root), key],
                               capture_output=True, timeout=3, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if reply.returncode or not reply.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            return 409, b'{"message":"Terminal frame unavailable; restore the registered CLI window."}', "application/json"
        return 200, reply.stdout, "image/png"
    except (OSError, ValueError, RuntimeError, PermissionError, sqlite3.Error, subprocess.TimeoutExpired, RecursionError):
        return 409, b'{"message":"No live registered CLI window for this session."}', "application/json"


def main() -> int:
    try:
        root, key = Path(sys.argv[1]), sys.argv[2]
        record = authorized(root, key)
        image = terminal_frame.capture(record["hwnd"])
        authorized(root, key)  # Ownership cannot change during capture.
        sys.stdout.buffer.write(image)
        return 0
    except (OSError, ValueError, RuntimeError, PermissionError, sqlite3.Error, RecursionError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
