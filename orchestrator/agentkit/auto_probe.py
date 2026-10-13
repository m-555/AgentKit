"""Re-prove write confinement by itself after an agent CLI or AgentKit's guards change (F31).

A cached probe authorizes writes only for the exact executable and guard code it
measured. Claude Code updates itself, and an edit to AgentKit's own hooks changes
the guard digest; either one held every writing task READY with "requires a fresh
functional confinement probe" until someone ran `agentkit probe --functional` by
hand, so nothing resumed after a usage-limit reset. The supervisor now runs that
same functional probe for the one adapter, in its own process, at most once per
hour for the same identity. A failed probe still withholds writes; it is
recorded, never bypassed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db
from .locking import atomic_write, exclusive

MARK = "requires a fresh functional confinement probe"
RETRY = timedelta(hours=1)


def _record(root, name: str) -> Path:
    return Path(root) / ".ai" / "runtime" / f"auto-probe-{name}.json"


def _identity(project, name: str) -> str | None:
    from . import adapters
    from .runtime_identity import identify
    adapter = adapters.get(name, project)
    install = adapter.detect() if adapter else None
    if install is None:
        return None
    return hashlib.sha256(json.dumps(identify(install), sort_keys=True).encode()).hexdigest()


def waiting(conn) -> set[str]:
    """Adapters whose READY tasks wait only for a fresh probe."""
    names = set()
    for task in db.list_tasks(conn, ("READY",)):
        blocker = str(task.get("blocker") or "")
        if MARK in blocker and blocker.split():
            names.add(blocker.split()[0])
    return names


def tick(conn, project, *, launch=None, now=None) -> list[str]:
    """Start a probe for each waiting adapter not tried for this identity within the hour."""
    notes = []
    moment = now or datetime.now(UTC)
    for name in sorted(waiting(conn)):
        identity = _identity(project, name)
        if identity is None:
            continue
        try:
            previous = json.loads(_record(project.root, name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        tried = db.parse_ts(previous.get("attempted_at"))
        if previous.get("identity") == identity and tried and moment - tried < RETRY:
            continue
        atomic_write(_record(project.root, name), json.dumps(
            {"identity": identity, "attempted_at": moment.isoformat()}) + "\n")
        (launch or _spawn)(project.root, name)
        db.log_event(conn, None, "auto_probe_started", cause=f"{name}: {MARK}",
                     detail={"adapter": name, "identity": identity[:16]})
        notes.append(f"{name}: confinement probe started for the changed runtime or guards")
    return notes


def _spawn(root, name: str) -> None:
    runtime = Path(root) / ".ai" / "runtime"
    with (runtime / f"auto-probe-{name}.log").open("a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, "-m", "agentkit.auto_probe", str(Path(root).resolve()), name],
                         cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_one(root, name: str, *, functional=None) -> dict:
    """The functional probe for one adapter, saved like `agentkit probe --functional`."""
    from . import adapters
    from .capabilities import load_cache, save_cache
    from .config import load_project
    from .probe import probe_functional, probe_static
    functional = functional or probe_functional
    for adapter, install in adapters.installed(load_project(root)):
        if adapter.name != name:
            continue
        caps = functional(adapter, install, probe_static(adapter, install),
                          fixture_root=Path(root) / ".ai/runtime/probes")
        cache = load_cache(root)
        cache[name] = caps
        save_cache(root, cache)
        return caps.to_dict()
    raise ValueError(f"adapter {name} is not installed")


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root, name = Path(args[0]), args[1]
    try:
        with exclusive(root, f"auto-probe-{name}", timeout=0):
            result = probe_one(root, name)
    except TimeoutError:
        return 0  # Another probe of this adapter is running.
    conn = db.connect(root)
    try:
        db.log_event(conn, None, "auto_probe_finished", cause=name,
                     detail={"adapter": name, "capabilities": result.get("capabilities")})
    finally:
        conn.close()
    print(json.dumps({"adapter": name, "result": result}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
