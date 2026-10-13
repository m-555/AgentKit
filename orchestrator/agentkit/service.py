"""Background supervisor lifecycle. The OS lock survives neither crash nor reboot."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

from .locking import exclusive

#: Passed to the background process when the cap should come from project config.
FROM_CONFIG = "config"


def _popen(argv, **options):
    """The one place the guardian is spawned; tests replace it.

    On Windows WMI creates it, outside the caller's process tree and job, so
    restarting the editor that ran `service start` cannot end it (F30). The
    guardian then writes its own log.
    """
    if os.name == "nt":
        from types import SimpleNamespace

        from .service_launch import wmi_create
        pid = wmi_create(argv, options.get("cwd") or Path.cwd())
        if pid:
            return SimpleNamespace(pid=pid)
    return subprocess.Popen(argv, **options)


def start(root, max_workers=None):
    """`max_workers=None` reads `.ai/project.yaml`; 0 is unlimited; a positive number caps."""
    from .concurrency import validate
    cap = FROM_CONFIG if max_workers is None else str(validate(max_workers))
    runtime = Path(root) / ".ai" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with exclusive(root, "service-start"):
        pidfile = runtime / "supervisor.pid"
        from .service_control import is_service, status
        # The health file names the guardian even when it was started without a
        # pidfile; a pidfile alone can name a dead or reused PID (F6).
        recorded = None
        with contextlib.suppress(OSError, ValueError):
            recorded = int(pidfile.read_text())
        if status(root)["guardian_alive"] or is_service(recorded, root, worker=False):
            return "Supervisor already running."
        argv = [sys.executable, "-m", "agentkit.service", str(Path(root).resolve()), cap]
        # Apart from the caller's console and job, so the guardian outlives the
        # shell or chat session that started it, and without a window (F34).
        from .service_launch import windowless_flags
        flags = windowless_flags()
        with (runtime / "supervisor.log").open("a", encoding="utf-8") as log:
            options = dict(cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=log,
                           stderr=log, start_new_session=os.name != "nt")
            try:
                process = _popen(argv, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0),
                                 **options)
            except OSError:  # the caller's job forbids breakaway
                process = _popen(argv, creationflags=flags, **options)
        pidfile.write_text(str(process.pid))
        limit = "project max_workers" if cap == FROM_CONFIG else ("unlimited" if cap == "0" else cap)
        return f"Supervisor started (pid {process.pid}, workers: {limit}); log: {runtime / 'supervisor.log'}"


def parse_cap(value: str) -> int | None:
    from .concurrency import validate
    return None if value == FROM_CONFIG else validate(value)


def _heal(root: Path, cap: str) -> None:
    """After each pass the supervisor restarts a guardian that died (F30); never fatal."""
    from .service_launch import heal
    from .view_keeper import heal as heal_view
    for check, name in ((lambda: heal(root, cap), "guardian"), (lambda: heal_view(root), "view")):
        try:
            note = check()
        except Exception as error:  # the supervisor must keep supervising
            note = f"{name} check failed: {error}"
        if note:
            print(note, flush=True)


if __name__ == "__main__":
    if "--worker" in sys.argv[3:]:
        from .watch import describe, run
        def report(state, result):
            print(f"pass {state.iterations}: {result.summary()}", flush=True)
            _heal(Path(sys.argv[1]), sys.argv[2])
        print(describe(run(Path(sys.argv[1]), max_workers=parse_cap(sys.argv[2]), on_pass=report)), flush=True)
    else:
        from .service_guardian import run as guard
        parse_cap(sys.argv[2])
        adopt = int(sys.argv[4]) if sys.argv[3:4] == ["--adopt"] else None
        with (Path(sys.argv[1]) / ".ai" / "runtime" / "supervisor.log").open("a", encoding="utf-8") as log:
            guard(Path(sys.argv[1]), sys.argv[2], adopt=adopt, out=log)
