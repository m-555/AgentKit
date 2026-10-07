"""Background supervisor lifecycle. The OS lock survives neither crash nor reboot."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .locking import exclusive

#: Passed to the background process when the cap should come from project config.
FROM_CONFIG = "config"


def start(root, max_workers=None):
    """`max_workers=None` reads `.ai/project.yaml`; 0 is unlimited; a positive number caps."""
    from .concurrency import validate
    cap = FROM_CONFIG if max_workers is None else str(validate(max_workers))
    runtime = Path(root) / ".ai" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with exclusive(root, "service-start"):
        pidfile = runtime / "supervisor.pid"
        from .reconcile import pid_alive
        if pidfile.exists():
            try:
                if pid_alive(int(pidfile.read_text())):
                    return "Supervisor already running."
            except ValueError:
                pass
        with (runtime / "supervisor.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "agentkit.service", str(Path(root).resolve()), cap],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt")
        pidfile.write_text(str(process.pid))
        limit = "project max_workers" if cap == FROM_CONFIG else ("unlimited" if cap == "0" else cap)
        return f"Supervisor started (pid {process.pid}, workers: {limit}); log: {runtime / 'supervisor.log'}"


def parse_cap(value: str) -> int | None:
    from .concurrency import validate
    return None if value == FROM_CONFIG else validate(value)


if __name__ == "__main__":
    if "--worker" in sys.argv[3:]:
        from .watch import describe, run
        def report(state, result):
            print(f"pass {state.iterations}: {result.summary()}", flush=True)
        print(describe(run(Path(sys.argv[1]), max_workers=parse_cap(sys.argv[2]), on_pass=report)), flush=True)
    else:
        from .service_guardian import run as guard
        parse_cap(sys.argv[2])
        guard(Path(sys.argv[1]), sys.argv[2])
