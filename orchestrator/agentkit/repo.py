"""Thin git helpers.

Kept deliberately small: AgentKit reads git state to build checkpoints and to
audit a finished diff against a lease, but it never rewrites history. Merges and
rebases belong to the integrator role, driven by explicit commands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import normalize


def _git(args: list[str], root: str | Path, timeout: int = 60, *, strict: bool = False) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        if strict:
            raise
        return ""
    if strict and proc.returncode:
        raise ValueError(f"git {args[0]} failed in {root}: {proc.stderr.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


def head_commit(root: str | Path) -> str:
    return _git(["rev-parse", "HEAD"], root).strip()


def current_branch(root: str | Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], root).strip()


def changed_files(root: str | Path) -> list[str]:
    """Uncommitted changes, staged or not, as repo-relative paths."""
    out = _git(["status", "--porcelain", "-z", "--untracked-files=all"], root, strict=True)
    files: list[str] = []
    records = iter(out.split("\0"))
    for line in records:
        if len(line) < 4:
            continue
        files.append(normalize(line[3:]))
        if "R" in line[:2] or "C" in line[:2]:
            original = next(records, "")
            if original:
                files.append(normalize(original))
    return files


def staged_files(root: str | Path) -> list[str]:
    """What a commit would contain right now — the L6 input."""
    out = _git(["diff", "--cached", "--no-renames", "--name-only", "-z"], root, strict=True)
    return [normalize(name) for name in out.split("\0") if name]


def diff_files(root: str | Path, base: str) -> list[str]:
    """Files changed since `base` — the L7 input.

    Uses two-dot range semantics deliberately: the merge gate must see everything
    the branch would bring in relative to the common ancestor, and `merge_base`
    is resolved by the caller so the comparison point is explicit.
    """
    out = _git(["diff", "--no-renames", "--name-only", "-z", f"{base}..HEAD"], root, strict=True)
    return [normalize(name) for name in out.split("\0") if name]


def merge_base(root: str | Path, a: str, b: str) -> str:
    return _git(["merge-base", a, b], root).strip()


def is_ancestor(root: str | Path, maybe_ancestor: str, descendant: str) -> bool:
    """True when `maybe_ancestor` is already contained in `descendant`.

    The idempotency check for merges (§14): merging an already-merged branch is a
    no-op, not an error.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def set_config(root: str | Path, key: str, value: str) -> None:
    _git(["config", key, value], root)


def rev_parse(root: str | Path, ref: str) -> str:
    return _git(["rev-parse", ref], root).strip()


def commits_since(root: str | Path, base: str) -> list[str]:
    """`<short sha> <subject>` for each commit after `base`."""
    out = _git(["log", "--oneline", "--no-decorate", f"{base}..HEAD"], root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def branch_exists(root: str | Path, branch: str) -> bool:
    return bool(_git(["rev-parse", "--verify", "--quiet", branch], root).strip())


def commit_exists(root: str | Path, sha: str) -> bool:
    """Is this commit still reachable? A rewritten history loses its old base."""
    if not sha:
        return False
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def tracked_files(root: str | Path) -> list[str]:
    out = _git(["ls-files"], root, timeout=120)
    return [normalize(line.strip()) for line in out.splitlines() if line.strip()]


def commit_file_sets(root: str | Path, days: int = 180, limit: int = 4000) -> list[list[str]]:
    """One list of changed files per commit — the co-change signal's raw input."""
    out = _git(
        ["log", f"--since={days}.days", f"--max-count={limit}", "--format=%x00",
         "--name-only", "--no-merges"],
        root, timeout=180,
    )
    commits: list[list[str]] = []
    current: list[str] = []
    for line in out.splitlines():
        if line.startswith("\x00"):
            if current:
                commits.append(current)
            current = []
            continue
        path = line.strip()
        if path:
            current.append(normalize(path))
    if current:
        commits.append(current)
    return commits


def file_commit_counts(root: str | Path, days: int = 180) -> dict[str, int]:
    out = _git(
        ["log", f"--since={days}.days", "--format=", "--name-only", "--no-merges"],
        root, timeout=180,
    )
    counts: dict[str, int] = {}
    for line in out.splitlines():
        path = line.strip()
        if path:
            key = normalize(path)
            counts[key] = counts.get(key, 0) + 1
    return counts


def is_clean(root: str | Path) -> bool:
    return not changed_files(root)


def worktree_list(root: str | Path) -> list[dict[str, str]]:
    out = _git(["worktree", "list", "--porcelain"], root)
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries
