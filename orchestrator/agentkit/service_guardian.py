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


def run(root, cap, *, spawn=None, sleeper=time.sleep, max_iterations=None):
    """An idle queue stays available; failures back off and remain visible."""
    root = Path(root).resolve()
    spawn = spawn or subprocess.Popen
    runtime = root / ".ai/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    health = runtime / "supervisor-health.json"
    failures = 0
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        state = {"guardian_pid": os.getpid(), "at": db.utcnow(),
                 "status": "starting", "iteration": iteration, "worker_cap": cap}
        atomic_write(health, json.dumps(state) + "\n")
        try:
            child = spawn([sys.executable, "-m", "agentkit.service", str(root), str(cap), "--worker"],
                          cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            state.update(status="running", supervisor_pid=child.pid)
            atomic_write(health, json.dumps(state) + "\n")
            code = child.wait()
            failures = failures + 1 if code else 0
            state.update(status="retrying" if code else "idle", exit_code=code)
        except OSError as error:
            failures += 1
            state.update(status="retrying", error=db.redact(str(error))[:1000])
        delay = min(300, 20 * 2 ** min(failures, 4)) if failures else 20
        state.update(at=db.utcnow(), restart_in_seconds=delay)
        atomic_write(health, json.dumps(state) + "\n")
        print(f"supervisor {state['status']}; host retry in {delay}s; AI owners preserved", flush=True)
        sleeper(delay)
