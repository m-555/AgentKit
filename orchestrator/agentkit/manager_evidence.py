"""Nonmutating Git and contract evidence for manager recovery."""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import globs, repo
from .config import load_project
from .worktree_digest import fingerprint


def integration(root, base=None) -> dict:
    from .integrator import integration_branch
    project = load_project(root)
    branch = integration_branch(project)
    if not repo.branch_exists(root, branch):
        return {"branch": branch, "head": None, "commits": [], "dirty_digest": None}
    head = repo._git(["rev-parse", f"refs/heads/{branch}"], root).strip()
    work = next((Path(e["worktree"]) for e in repo.worktree_list(root)
                 if e.get("branch") == f"refs/heads/{branch}"), None)
    return {"branch": branch, "head": head, "commits": repo._git(["log", "--format=%H", f"{base}..{head}"], root).splitlines() if base else [],
            "dirty_digest": fingerprint(work) if work and work.exists() else None,
            "contracts": contracts(work or Path(root), commit=None if work else head)}


def contracts(root, *, commit=None) -> dict:
    project = load_project(root)
    if commit:
        paths = repo._git(["ls-tree", "-r", "--name-only", "-z", commit], root).split("\0")
    else:
        paths = repo._git(["ls-files", "-z"], root).split("\0")
    result = {}
    for name in paths:
        if not name or not globs.matches_any(project.contracts, name):
            continue
        target = Path(root) / name
        if commit:
            content = repo._git(["show", f"{commit}:{name}"], root).encode()
        elif target.is_symlink():
            content = b"[symlink withheld]"
        elif target.is_file():
            content = target.read_bytes()
        else:
            content = b"[missing]"
        result[name] = hashlib.sha256(content).hexdigest()
    return result
