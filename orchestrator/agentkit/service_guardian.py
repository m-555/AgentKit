"""Keep the host supervisor available through crashes, without restarting AI owners."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .locking import atomic_write

#: Written by `agentkit service reload`: the next supervisor exit is a requested
#: restart for new code, not a crash, so it restarts at once with no backoff.
RELOAD_MARKER = "supervisor-reload"


def _adopted(health, marker, supervisor, sleeper, out) -> None:
    """A replacement guardian watches the supervisor it found running until it exits (F30)."""
    from .reconcile import pid_alive
    state = {"guardian_pid": os.getpid(), "at": db.utcnow(), "status": "running", "iteration": 0,
             "supervisor_pid": supervisor, "adopted": True}
    atomic_write(health, json.dumps(state) + "\n")
    while pid_alive(supervisor):
        sleeper(5)
    reloaded = marker.exists()
    marker.unlink(missing_ok=True)
    delay = 1 if reloaded else 20
    state.update(status="reloading" if reloaded else "retrying", at=db.utcnow(), restart_in_seconds=delay)
    atomic_write(health, json.dumps(state) + "\n")
    print(f"adopted supervisor {supervisor} exited; host retry in {delay}s", file=out, flush=True)
    sleeper(delay)


def run(root, cap, *, spawn=None, sleeper=time.sleep, max_iterations=None, adopt=None, out=None):
    """An idle queue stays available; failures back off and remain visible."""
    root = Path(root).resolve()
    spawn = spawn or subprocess.Popen
    out = out or sys.stdout
    runtime = root / ".ai/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    health = runtime / "supervisor-health.json"
    marker = runtime / RELOAD_MARKER
    if adopt:
        _adopted(health, marker, adopt, sleeper, out)
    redirect = {"stdout": out, "stderr": out} if hasattr(out, "fileno") and out is not sys.stdout else {}
    failures = 0
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        state = {"guardian_pid": os.getpid(), "at": db.utcnow(),
                 "status": "starting", "iteration": iteration, "worker_cap": cap}
        atomic_write(health, json.dumps(state) + "\n")
        reloaded = False
        try:
            child = spawn([sys.executable, "-m", "agentkit.service", str(root), str(cap), "--worker"],
                          cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **redirect)
            state.update(status="running", supervisor_pid=child.pid)
            atomic_write(health, json.dumps(state) + "\n")
            code = child.wait()
            reloaded = marker.exists()
            marker.unlink(missing_ok=True)
            failures = 0 if reloaded else (failures + 1 if code else 0)
            state.update(status="reloading" if reloaded else ("retrying" if code else "idle"), exit_code=code)
        except OSError as error:
            failures += 1
            state.update(status="retrying", error=db.redact(str(error))[:1000])
        delay = 1 if reloaded else (min(300, 20 * 2 ** min(failures, 4)) if failures else 20)
        state.update(at=db.utcnow(), restart_in_seconds=delay)
        atomic_write(health, json.dumps(state) + "\n")
        print(f"supervisor {state['status']}; host retry in {delay}s; AI owners preserved", file=out, flush=True)
        sleeper(delay)
