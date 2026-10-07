"""One optional Windows log viewer per running process; never owns agent work."""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

from . import live, public_activity, terminal_mirror
from .config import load_project


def spawn(root, identifier: int) -> bool:
    """Start a disposable console only when visible_windows is explicitly enabled."""
    if os.name != "nt" or load_project(root).raw.get("visible_windows", False) is not True:
        return False
    startup = subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None
    if startup is not None:
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # SW_SHOWNORMAL: user explicitly requested visible workers.
    diagnostic = Path(root) / ".ai/runtime" / f"process-{identifier}" / "viewer.stderr.log"
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    with diagnostic.open("ab") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "agentkit.windows", str(Path(root).resolve()), str(identifier)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            startupinfo=startup, stderr=errors,
        )
    return True


def selected(view: dict, identifier: int) -> dict | None:
    row = next((p for p in view["processes"] if p["id"] == identifier), None)
    if row is None or not (row["monitor_alive"] or row["child_alive"] or row.get("ownership_uncertain")):
        return None
    return {**view, "processes": [row], "external_managers": [],
            "tasks": [t for t in view["tasks"] if t["id"] == row["task_id"]]}


def read_snapshot(root, identifier):
    return live.snapshot(root, process_id=identifier)


def _display(frame):
    print(frame, flush=True)


def watch(root, identifier: int, *, poll_seconds=2.0, output=_display,
          sleeper=time.sleep, reader=read_snapshot, title=None) -> int:
    """Exit when the assigned monitor and child stop. Never kill either process."""
    if identifier < 1 or not math.isfinite(poll_seconds) or poll_seconds < 0.1:
        return 2
    previous: dict | str | None = None
    shown: set[str] = set()
    try:
        while True:
            view = reader(root, identifier)
            if view["status"] == "unavailable":
                # A busy DB is not evidence that an agent has stopped.
                message = f"AgentKit #{identifier}: {view.get('message', 'Runtime unavailable')} Retrying."
                if previous != message:
                    output(message)
                    previous = message
                sleeper(poll_seconds)
                continue
            active = selected(view, identifier)
            if active is None:
                return 0
            row = active["processes"][0]
            if title:
                requested = row.get("requested_effort", "unknown")
                reported = row.get("observed_effort", "unknown")
                title(f"{terminal_mirror.prefix(Path(root), identifier)}{row['role']} {row['model']} "
                      f"requested {requested}; reported {reported}")
                if reader is read_snapshot:
                    terminal_mirror.register_viewer(Path(root), identifier)
            # Avoid repeating an identical frame solely because the clock advanced.
            current = {**active, "at": "current"}
            if current != previous:
                output(live.render(active))
                previous = current
            stream = public_activity.read(Path(root), identifier)
            for message in stream["messages"]:
                if message["id"] not in shown:
                    output(f"[{message['kind']}] {message['text']}")
                    shown.add(message["id"])
            if len(shown) > 2048:
                shown = {message["id"] for message in stream["messages"]}
            sleeper(poll_seconds)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("process", type=int)
    args = parser.parse_args(argv)
    title = None
    if os.name == "nt":
        import ctypes
        title = ctypes.windll.kernel32.SetConsoleTitleW
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        # The supervisor redirects its stdout. A new console must bind its own
        # output rather than inheriting that file/pipe and displaying nothing.
        with open("CONOUT$", "w", encoding="utf-8", buffering=1) as console:
            return watch(Path(args.root), args.process, title=title,
                         output=lambda frame: print(frame, file=console, flush=True))
    return watch(Path(args.root), args.process, title=title)


if __name__ == "__main__":
    raise SystemExit(main())
