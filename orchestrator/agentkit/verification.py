"""Reuse passing checks only at the same clean commit and verification inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import db, gates, repo, workflow, worktrees


def signature(project, worktree, level):
    root = Path(worktree)
    manifests = [root / "node_modules/.package-lock.json"]
    for base in (root / ".venv", root / "venv"):
        manifests += list(base.glob("Lib/site-packages/*.dist-info/RECORD"))
        manifests += list(base.glob("lib/python*/site-packages/*.dist-info/RECORD"))
    installed = [(str(path.relative_to(root)), path.stat().st_mtime_ns, path.stat().st_size)
                 for path in sorted(manifests) if path.is_file()]
    from .environment_profiles import select
    profile = select(project, level=level)
    inputs = [profile, str(root.resolve()), project.gate(level), project.worktree_setup, installed,
              worktrees.environment_fingerprint(worktree), project.raw.get("playwright_checks", [])]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def cached(conn, project, worktree, level):
    head = repo.head_commit(worktree)
    if not head or not repo.is_clean(worktree):
        return None
    row = conn.execute("SELECT * FROM verification_cache WHERE head_sha=? AND signature=? AND level=?",
                       (head, signature(project, worktree, level), level)).fetchone()
    return dict(row) if row and row["passed"] else None


def run(conn, project, worktree, level, *, force=False):
    head = repo.head_commit(worktree)
    if not head or not repo.is_clean(worktree):
        return gates.GateResult(level, False, skipped_reason="a clean committed checkout is required")
    key = signature(project, worktree, level)
    reuse = workflow.enabled(project) and (project.raw.get("workflow") or {}).get("cache_checks", True) is True
    previous = cached(conn, project, worktree, level) if reuse and not force else None
    if previous:
        return gates.GateResult(level, True, skipped_reason="reused verified PASS at " + head)
    result = gates.run_gate(project, level, cwd=worktree)
    stable = repo.head_commit(worktree) == head and repo.is_clean(worktree)
    passed = result.passed and stable
    if not stable:
        changed = repo.changed_files(worktree)
        result.passed = False
        result.results.append(gates.CommandResult(
            "committed checkout integrity", 1, 0,
            "Check changed committed checkout; paths: " + ", ".join(changed)
            + "; original head: " + head + "; current head: " + repo.head_commit(worktree)))
    conn.execute("INSERT INTO verification_cache(head_sha,signature,level,passed,summary,checked_at) "
                 "VALUES(?,?,?,?,?,?) ON CONFLICT(head_sha,signature,level) DO UPDATE SET "
                 "passed=excluded.passed,summary=excluded.summary,checked_at=excluded.checked_at",
                 (head, key, level, int(passed), db.redact(result.summary()), db.utcnow()))
    return result
