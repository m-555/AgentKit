"""Grouped checkout storage; reservations remain authoritative until explicit migration."""
from __future__ import annotations

import re
from pathlib import Path

from .config import load_project


def directory(root, project=None):
    root = Path(root).resolve()
    project = project or load_project(root)
    configured = project.raw.get("worktree_root")
    if configured:
        target = Path(str(configured)).expanduser()
        if not target.is_absolute():
            raise ValueError("worktree_root must be an absolute external directory")
        target = target.resolve()
        if target == root or root in target.parents or target in root.parents:
            raise ValueError("worktree_root must be outside the source checkout and its ancestors")
        if target.exists() and not target.is_dir():
            raise ValueError("worktree_root is not a directory")
        return target
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-") or "project"
    return root.parent / f"wt-{name}"


def destination(root, task, project=None):
    from .workspace_registry import suffix
    from .worktrees import slug
    return directory(root, project) / f"{slug(task)}-{suffix(task)}"
