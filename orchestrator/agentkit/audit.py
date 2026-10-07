"""L5, L6 and L7 — the layers that make the guarantee complete.

L3/L4 guard the channels they can see. These three see everything, because they
ask git what actually changed rather than asking the agent what it intended:

* **L5 audit** (DETECTION) — every mutation inside the worktree, whatever the
  channel: shell, interpreter, formatter, package manager, build system.
* **L6 pre-commit** (PREVENTION at the commit boundary) — nothing out of lease
  enters history.
* **L7 merge gate** (ENFORCEMENT, absolute) — nothing out of lease reaches the
  integration branch. This is the one guarantee PLAN_V3 §2.3 states without
  qualification, and it holds for agents with no hook system at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, globs, repo
from .config import ProjectConfig
from .leases import decide
from .paths import normalize

#: Never audited: outputs are regenerated on the integration branch (§11.4).
DEFAULT_GENERATED = (
    "**/*.generated.*", "**/__pycache__/**", "**/*.pyc", "**/node_modules/**",
    "**/.pytest_cache/**", "**/.ruff_cache/**", "**/.mypy_cache/**",
    "dist/**", "build/**", ".ai/tasks.db*", ".ai/capabilities.json",
    ".ai/runtime/**", "**/.venv/**",
)


@dataclass
class Violation:
    path: str
    reason: str
    code: str
    owner_task: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason, "code": self.code,
                "owner_task": self.owner_task}


@dataclass
class AuditResult:
    task_id: int | None
    checked: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    layer: str = "L5"

    @property
    def clean(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer, "task": self.task_id, "clean": self.clean,
            "checked": len(self.checked),
            "violations": [v.to_dict() for v in self.violations],
        }

    def summary(self) -> str:
        if self.clean:
            return f"{self.layer}: clean ({len(self.checked)} file(s) checked)"
        lines = [f"{self.layer}: {len(self.violations)} out-of-lease change(s)"]
        for violation in self.violations:
            lines.append(f"  {violation.path}")
            lines.append(f"    {violation.reason}")
        return "\n".join(lines)


def generated_patterns(project: ProjectConfig) -> list[str]:
    declared = project.raw.get("generated") if project.raw else None
    extra = [str(p) for p in (declared or [])]
    return [*DEFAULT_GENERATED, *extra]


def _is_generated(path: str, patterns: list[str]) -> bool:
    """Match the path, and also the path as a directory.

    `git status` reports an untracked *directory* as a single entry
    (`services/__pycache__`), so a pattern written for its contents
    (`**/__pycache__/**`) would otherwise miss it and report the whole directory
    as an out-of-lease change.
    """
    if globs.matches_any(patterns, path) is not None:
        return True
    return globs.matches_any(patterns, f"{path}/_") is not None


def _check(
    conn: sqlite3.Connection, project: ProjectConfig, paths: list[str], task_id: int | None,
) -> list[Violation]:
    patterns = generated_patterns(project)
    violations: list[Violation] = []
    for path in paths:
        rel = normalize(path)
        if not rel or _is_generated(rel, patterns):
            continue
        verdict = decide(conn, project, rel, task_id)
        if not verdict.allowed:
            violations.append(
                Violation(rel, verdict.reason, verdict.code, verdict.owner_task)
            )
    return violations


def audit_worktree(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    worktree: str | Path,
    task_id: int | None,
    *,
    record: bool = True,
) -> AuditResult:
    """L5 — everything changed in this worktree, by any means.

    Uncommitted changes plus anything already committed since the task's base.
    Cost is one `git status` plus one `git diff --name-only`.
    """
    task = db.get_task(conn, task_id) if task_id is not None else None
    changed = set(repo.changed_files(worktree))
    base = (task or {}).get("base_sha")
    if base:
        changed.update(repo.diff_files(worktree, str(base)))

    paths = sorted(changed)
    violations = _check(conn, project, paths, task_id)
    result = AuditResult(task_id=task_id, checked=paths, violations=violations, layer="L5")

    if record and violations:
        for violation in violations:
            db.record_violation(
                conn, task_id, "L5", violation.path, violation.reason, channel="worktree_audit",
            )
    return result


def audit_staged(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    worktree: str | Path,
    task_id: int | None,
) -> AuditResult:
    """L6 — what this commit would contain."""
    paths = repo.staged_files(worktree)
    violations = _check(conn, project, paths, task_id)
    return AuditResult(task_id=task_id, checked=paths, violations=violations, layer="L6")


def audit_branch(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    worktree: str | Path,
    task_id: int,
    base: str,
) -> AuditResult:
    """L7 — the complete diff a merge would bring in. The authority."""
    paths = repo.diff_files(worktree, base)
    violations = _check(conn, project, paths, task_id)
    result = AuditResult(task_id=task_id, checked=paths, violations=violations, layer="L7")
    if violations:
        for violation in violations:
            db.record_violation(
                conn, task_id, "L7", violation.path, violation.reason, channel="merge_gate",
            )
        db.log_event(
            conn, task_id, "merge_rejected",
            cause="out-of-lease changes in the branch diff",
            effect="branch quarantined; not merged",
            detail={"violations": [v.to_dict() for v in violations], "base": base},
        )
    return result


# ------------------------------------------------------------------ L6 install


PRE_COMMIT_TEMPLATE = """#!/bin/sh
# AgentKit L6 — refuse commits containing changes outside this task's lease.
# Installed per worktree; not a repository-wide hook.
exec {python} -m agentkit.hooks_cli pre-commit
"""


def install_pre_commit(worktree: str | Path, orchestrator: str | Path) -> Path:
    """Install the L6 hook scoped to one worktree.

    `core.hooksPath` is set on the worktree's own config, so this never changes
    the developer's main checkout.
    """
    work = Path(worktree)
    import shlex
    import sys
    hooks_dir = work / ".ai" / "githooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text(
        PRE_COMMIT_TEMPLATE.format(python=shlex.quote(sys.executable.replace("\\", "/"))),
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    import subprocess
    # --worktree is essential: ordinary git config changes the shared .git/config.
    subprocess.run(["git", "config", "extensions.worktreeConfig", "true"], cwd=work, check=True)
    subprocess.run(["git", "config", "--worktree", "core.hooksPath", ".ai/githooks"], cwd=work, check=True)
    return hook
