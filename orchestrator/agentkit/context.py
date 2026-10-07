"""Working out which task the current session belongs to.

A worker is told its task id through the `AGENTKIT_TASK` environment variable,
set by the launcher. When that is absent the session is an ordinary interactive
one and AgentKit stays out of the way entirely — that is what makes the plugin
safe to install globally while only some repos use it.

A worktree is also a strong signal: a worker running in `../wt-task-101` belongs
to task 101 even if the variable was lost across a process restart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import find_project_root

_WORKTREE_TASK = re.compile(r"(?:^|[^0-9a-z])task[-_]?(\d+)", re.IGNORECASE)


def active_task_id(cwd: str | Path | None = None) -> int | None:
    raw = os.environ.get("AGENTKIT_TASK", "").strip()
    if raw.isdigit():
        return int(raw)

    here = Path(cwd or os.getcwd())
    for part in (here.name, *(p.name for p in here.parents)):
        match = _WORKTREE_TASK.search(part)
        if match:
            return int(match.group(1))
    return None


def active_generation(cwd: str | Path | None = None) -> int | None:
    """The generation this worker was launched at.

    A worker whose generation is behind the task's current one is a zombie from a
    previous attempt, and core rejects its writes (PLAN_V3 §14).
    """
    raw = os.environ.get("AGENTKIT_GENERATION", "").strip()
    return int(raw) if raw.isdigit() else None


def project_root(cwd: str | Path | None = None) -> Path | None:
    return find_project_root(cwd)


def is_managed(cwd: str | Path | None = None) -> bool:
    """True when this directory belongs to a repo that has been onboarded."""
    root = find_project_root(cwd)
    return bool(root and (root / ".ai" / "project.yaml").is_file())
