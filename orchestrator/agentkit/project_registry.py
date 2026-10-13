"""Repositories that use AgentKit, so one view can list, configure and start any of them.

The list lives in the user's AgentKit home (AGENTKIT_HOME, default ~/.agentkit), never in
a project. `agentkit init`, `agentkit view` and `agentkit service start` record their
project; listing also finds sibling repositories that already have .ai/project.yaml, so
repositories onboarded before the list existed appear too. Git worktrees (a `.git`
file instead of a directory) are task checkouts and are never listed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .locking import atomic_write


def home() -> Path:
    return Path(os.environ.get("AGENTKIT_HOME") or Path.home() / ".agentkit")


def _path() -> Path:
    return home() / "projects.json"


def is_project(root) -> bool:
    root = Path(root)
    return (root / ".ai" / "project.yaml").is_file() and (root / ".git").is_dir()


def ident(root) -> str:
    """A stable short id for a repository path; case-insensitive where the OS is."""
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode("utf-8")).hexdigest()[:12]


def _load() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and isinstance(value.get("projects"), dict) else {}


def register(root) -> None:
    """Remember a repository and when it was last used. Best effort: the list is a convenience."""
    if not is_project(root):
        return
    resolved = str(Path(root).resolve())
    data = _load() or {"projects": {}}
    # A sequence, not the clock, orders recent use: registrations inside one clock tick tie.
    sequence = 1 + max((entry.get("sequence") or 0 for entry in data["projects"].values()), default=0)
    data["projects"][ident(resolved)] = {"root": resolved, "last_used_at": datetime.now(UTC).isoformat(),
                                         "sequence": sequence}
    with contextlib.suppress(OSError):
        atomic_write(_path(), json.dumps(data, indent=2) + "\n")


def projects() -> list[dict]:
    """Registered repositories and their AgentKit siblings, most recently used first."""
    found: dict[str, dict] = {}
    for entry in _load().get("projects", {}).values():
        root = Path(str(entry.get("root", "")))
        if entry.get("root") and is_project(root):
            found[ident(root)] = {"root": str(root.resolve()), "last_used_at": entry.get("last_used_at"),
                                  "sequence": entry.get("sequence") or 0}
    for parent in {Path(entry["root"]).parent for entry in found.values()}:
        with contextlib.suppress(OSError):
            for child in sorted(parent.iterdir()):
                if child.is_dir() and ident(child) not in found and is_project(child):
                    found[ident(child)] = {"root": str(child.resolve()), "last_used_at": None, "sequence": 0}
    rows = [{"id": key, "name": Path(value["root"]).name, **value} for key, value in found.items()]
    return sorted(rows, key=lambda row: (-row["sequence"], row["name"].lower()))


def resolve(identifier: str) -> Path | None:
    """The listed repository with this id; never an arbitrary path from a request."""
    return next((Path(row["root"]) for row in projects() if row["id"] == identifier), None)
