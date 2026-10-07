"""Operator cancellation is durable; a watcher cannot turn a stop into a wake."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .locking import atomic_write


def path(root, thread):
    return Path(root) / ".ai/runtime/native-wake-control" / (hashlib.sha256(thread.encode()).hexdigest()[:24] + ".json")


def cancel(root, thread, reason):
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Only the host/operator can cancel native wake")
    if not thread or not reason.strip():
        raise ValueError("Thread and cancellation reason are required")
    value = {"cancelled": True, "reason": reason[:1000]}
    atomic_write(path(root, thread), json.dumps(value) + "\n")
    return value


def require_enabled(root, thread):
    marker = path(root, thread)
    if marker.exists():
        raise RuntimeError("Native wake cancelled by operator: " + json.loads(marker.read_text()).get("reason", "stop"))
    from .config import load_project
    if load_project(root).raw.get("execution_paused") is True:
        raise RuntimeError("Project execution is paused; cancel native wake")


def archive_finished(root, directory, key):
    """Explicit renewed authorization archives a proven terminal one-shot only."""
    import uuid
    root, directory = Path(root).resolve(), Path(directory).resolve()
    if not directory.is_relative_to(root):
        raise PermissionError("Wake history must remain inside the target project")
    result_path = directory / f"{key}.result.json"
    if not result_path.exists():
        raise RuntimeError("Previous wake outcome is missing; reconcile before renewing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    terminal = {"same_native_chat_turn_completed", "delivered_turn_failed_or_interrupted",
                "native_delivery_rejected_no_retry", "cancelled_no_delivery", "deadline_reached_no_delivery"}
    if result.get("status") not in terminal:
        raise RuntimeError("Previous wake outcome is unresolved; cannot renew")
    archive = directory / "archive" / uuid.uuid4().hex
    archive.mkdir(parents=True)
    for suffix in ("armed", "claimed", "result"):
        path = directory / f"{key}.{suffix}.json"
        if path.exists():
            if not path.resolve().is_relative_to(root):
                raise PermissionError("Wake history path escaped the project")
            path.rename(archive / path.name)
    return archive
