"""Cross-project capacity reservations before host provisioning; no worker attempts."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from .locking import atomic_write, exclusive
from .reconcile import pid_alive


def cache_root(project):
    configured = project.raw.get("dependency_cache_root")
    path = Path(str(configured)).expanduser() if configured else project.root.parent / ".agentkit-cache"
    if not path.is_absolute():
        raise ValueError("dependency_cache_root must be absolute")
    return path


@contextmanager
def reserve(project, work, profile):
    cache = cache_root(project)
    cache.mkdir(parents=True, exist_ok=True)
    file = cache / ".ai/runtime/capacity.json"
    token = uuid.uuid4().hex
    with exclusive(cache, "environment-capacity"):
        previous = json.loads(file.read_text()) if file.exists() else {}
        active = {k: v for k, v in previous.items() if pid_alive(v["pid"])}
        required = profile["reserve_bytes"]
        available = shutil.disk_usage(work).free - sum(v["bytes"] for v in active.values())
        if available < profile["min_free_bytes"] + required:
            raise OSError(28, "Insufficient free disk space after active environment reservations")
        active[token] = {"pid": os.getpid(), "bytes": required, "worktree": str(work)}
        atomic_write(file, json.dumps(active) + "\n")
    try:
        yield
    finally:
        with exclusive(cache, "environment-capacity"):
            active = json.loads(file.read_text()) if file.exists() else {}
            active.pop(token, None)
            atomic_write(file, json.dumps(active) + "\n")
