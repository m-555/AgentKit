"""Separate Claude worker filesystem coordinates from primary lease authority.

Expected assignment/generation failures block; unrelated runtime errors retain
the hook facade's existing fail-open contract. This leaf imports no facade.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, worker
from .paths import canonical_relpath, is_absolute_like


class HookContextError(ValueError):
    """Known invalid or stale worker state: refusal, not a callback failure."""


def supervised() -> bool:
    return any(os.environ.get(key) for key in ("AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE")) or bool(
        os.environ.get("AGENTKIT_TASK") and os.environ.get("AGENTKIT_ROOT"))


def _directory(value: Any, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
        raise HookContextError(f"{label} is missing or malformed")
    path = Path(value)
    if not path.is_absolute():
        raise HookContextError(f"{label} must be an absolute native path")
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise HookContextError(f"{label} is not a directory")
        return path
    except (OSError, RuntimeError) as error:
        raise HookContextError(f"{label} is unavailable") from error


def authority_root(discovered: Path | None, task_id: int | None) -> Path | None:
    """Use existing primary state; never create a worker-side or missing DB."""
    if not supervised():
        return discovered
    raw_task = os.environ.get("AGENTKIT_TASK", "")
    raw_generation = os.environ.get("AGENTKIT_GENERATION", "")
    if (not raw_task.isascii() or not raw_task.isdigit() or int(raw_task) != task_id
            or not raw_generation.isascii() or not raw_generation.isdigit()):
        raise HookContextError("Supervised task and generation must be explicit valid integers")
    root = _directory(os.environ.get("AGENTKIT_ROOT") or discovered, "Primary authority root")
    if discovered is not None and _directory(discovered, "Discovered primary root") != root:
        raise HookContextError("Hook project disagrees with the assigned primary authority")
    if not (root / ".ai/project.yaml").is_file() or not (root / ".ai/tasks.db").is_file():
        raise HookContextError("Assigned primary config or task database is unavailable")
    return root


def validate_cwd(payload: dict[str, Any]) -> None:
    if supervised():
        _directory(payload.get("cwd"), "Supervised hook cwd")


@dataclass(frozen=True)
class WriteContext:
    worktree: Path
    cwd: Path
    generation: int | None = None

    def relative(self, raw: str) -> str | None:
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            return None
        candidate = Path(raw)
        if is_absolute_like(raw):
            if not candidate.is_absolute():
                return None
        else:
            candidate = self.cwd / candidate
        return canonical_relpath(candidate, self.worktree)


def write_context(conn: sqlite3.Connection, root: Path, task_id: int | None,
                  payload: dict[str, Any]) -> WriteContext:
    """Validate the current assignment, then map targets in that checkout only."""
    managed_worker = supervised()
    if task_id is None:
        if managed_worker:
            raise HookContextError("Supervised worker has no task assignment")
        return WriteContext(root, root)
    generation = int(os.environ["AGENTKIT_GENERATION"]) if managed_worker else None
    task: dict[str, Any] | None
    if managed_worker:
        try:
            task = worker.require_current(conn, task_id, worker_generation=generation).task
        except (worker.StaleGeneration, ValueError, PermissionError) as error:
            raise HookContextError(str(error)) from error
        if str(task.get("status")) not in db.ACTIVE_STATES:
            raise HookContextError("Assigned worker task is no longer active")
    else:
        task = db.get_task(conn, task_id)
    assigned = task.get("worktree") if task else None
    if not assigned:
        if managed_worker:
            raise HookContextError("Supervised task has no assigned worktree")
        return WriteContext(root, root)
    worktree = _directory(assigned, "Authoritative task worktree")
    expected = os.environ.get("AGENTKIT_WORKTREE")
    if expected and _directory(expected, "Launcher worktree") != worktree:
        raise HookContextError("Launcher and authoritative task worktrees disagree")
    raw_cwd = payload.get("cwd") if managed_worker else payload.get("cwd") or payload.get("workspace") or payload.get("worktree")
    cwd = _directory(raw_cwd, "Hook cwd")
    if cwd != worktree and canonical_relpath(cwd, worktree, allow_missing=False) is None:
        raise HookContextError("Hook cwd is outside the assigned task worktree")
    return WriteContext(worktree, cwd, generation)
