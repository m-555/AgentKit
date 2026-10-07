"""Host-written usage receipts survive viewer restarts and event-log retirement."""
from __future__ import annotations

import json
from pathlib import Path

from .locking import atomic_write
from .session_usage import scan_details

FIELDS = ("input_tokens", "output_tokens", "thinking_tokens",
          "cached_input_tokens", "cache_write_input_tokens")


def path_for(root: Path, identifier: int, name: str) -> Path:
    if type(identifier) is not int or identifier <= 0 or name not in ("usage.json", "events.jsonl"):
        raise ValueError("invalid usage source")
    project = root.resolve(strict=True)
    runtime = (project / ".ai" / "runtime").resolve(strict=True)
    runtime.relative_to(project)
    path = runtime / f"process-{identifier}" / name
    path.resolve().relative_to(runtime)
    path.resolve().relative_to(project)
    return path


def fingerprint(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def read(root: Path, identifier: int) -> dict | None:
    try:
        path = path_for(root, identifier, "usage.json")
        if path.stat().st_size > 8192:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        usage = record.get("usage")
        if record.get("version") != 1 or record.get("process_id") != identifier or not isinstance(usage, dict):
            return None
        if any(usage.get(key) is not None and
               (type(usage[key]) is not int or usage[key] < 0) for key in FIELDS):
            return None
        return record
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return None


def capture(root: Path, identifier: int) -> bool:
    """No inference, no task changes. Repeated capture replaces, never adds."""
    try:
        source = path_for(root, identifier, "events.jsonl")
        before = fingerprint(source)
        previous = read(root, identifier)
        if previous and previous.get("fingerprint") == before:
            return False
        usage = scan_details(root, identifier)
        if before != fingerprint(source):
            return False  # An active writer changed the source; retry after it stops.
        receipt = {"version": 1, "process_id": identifier,
                   "fingerprint": before, "usage": usage}
        atomic_write(path_for(root, identifier, "usage.json"), json.dumps(receipt) + "\n")
        return True
    except (OSError, ValueError, RuntimeError, TypeError):
        return False  # Observability must never break worker completion.


def capture_stopped(root: Path, conn, *, limit: int = 2) -> int:
    updated = 0
    for row in conn.execute("SELECT id FROM processes WHERE status NOT IN ('STARTING','RUNNING') ORDER BY id DESC"):
        if capture(root, row[0]):
            updated += 1
            if updated >= limit:
                break
    return updated
