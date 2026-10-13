"""Keep the view online: the supervisor restarts it like the guardian (always-on view).

The user wants the view always available and able to send. `agentkit view`
records the wish in .ai/runtime/view.json; after every pass the supervisor (which
itself heals, F30) starts the view again whenever nothing answers on its port.
A view running without operator controls is replaced, so Send always works.
`agentkit view --stop` clears the wish and stops it.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .locking import atomic_write

RESTART_GAP = timedelta(seconds=60)


def _state_path(root) -> Path:
    return Path(root) / ".ai" / "runtime" / "view.json"


def state(root) -> dict:
    try:
        value = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(root, value: dict) -> None:
    _state_path(root).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_state_path(root), json.dumps(value) + "\n")


def probe(port: int) -> dict | None:
    """What answers on the port: {"pid", "root", "operator_controls", "started_at"}, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/view", timeout=2) as reply:
            value = json.loads(reply.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def launch(root, port: int) -> int | None:
    """Start the view with operator controls, outside the caller's process tree."""
    argv = [sys.executable, "-m", "agentkit.dashboard", "--root", str(Path(root).resolve()),
            "--port", str(port), "--operator-controls", "--home"]
    cwd = Path(__file__).resolve().parents[1]
    if os.name == "nt":
        from .service_launch import wmi_create
        pid = wmi_create(argv, cwd)
        if pid:
            return pid
    from .service_launch import windowless_flags
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=windowless_flags(),
                            start_new_session=os.name != "nt").pid


def end(pid: int) -> None:
    """End a view process and its launcher tree."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.kill(pid, 15)


def ensure(root, port: int, *, starter=launch, prober=probe, stopper=end, wait=True) -> str:
    """A sending view of every repository on the port: keep it, replace this project's older one, or start one."""
    current = prober(port)
    resolved = str(Path(root).resolve())
    if current and current.get("operator_controls") and current.get("home"):
        return "running"
    if current and current.get("root") == resolved and isinstance(current.get("pid"), int):
        stopper(current["pid"])  # this project's single or read-only view: replace it with the shared one
        time.sleep(1 if wait else 0)
    elif current:
        return "port busy"  # another project's view; never touch it
    pid = starter(root, port)
    for _ in range(40 if wait else 0):
        if prober(port):
            break
        time.sleep(0.25)
    return f"started {pid}"


def heal(root, *, starter=launch, prober=probe, stopper=end, now=None) -> str | None:
    """Called by the supervisor after every pass: bring a wanted view back."""
    wish = state(root)
    if not wish.get("always_on"):
        return None
    port = int(wish.get("port") or 8766)
    current = prober(port)
    if current and current.get("operator_controls"):
        return None
    moment = now or datetime.now(UTC)
    last = wish.get("restarted_at")
    if last and moment - datetime.fromisoformat(last) < RESTART_GAP:
        return None  # a start is under way
    result = ensure(root, port, starter=starter, prober=prober, stopper=stopper, wait=False)
    save(root, {**wish, "restarted_at": moment.isoformat()})
    return f"view on port {port}: {result}"
