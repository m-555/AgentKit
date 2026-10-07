"""Keep host-generated, untracked guard files out of repository changes.

This uses Git's local exclude file, never a project's versioned .gitignore.
Tracked files still appear in audits; forced staging is still checked by L6/L7.
Runtime Codex enforcement uses the separately reviewed session hook definition.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def exclude_untracked(worktree: Path, *paths: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=worktree,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        return  # Disposable adapter probes can run outside Git.
    target = Path(result.stdout.strip())
    if not target.is_absolute():
        target = worktree / target
    previous = target.read_text(encoding="utf-8") if target.exists() else ""
    entries = set(previous.splitlines())
    additions = ["/" + path for path in paths if "/" + path not in entries]
    if additions:
        target.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if not previous or previous.endswith("\n") else "\n"
        target.write_text(previous + separator + "\n".join(additions) + "\n", encoding="utf-8")
