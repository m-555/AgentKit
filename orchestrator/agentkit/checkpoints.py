"""Mechanical and semantic checkpoints (PLAN_V3 §9).

v2 relied on a context-exhausted model writing a good handoff — the least
reliable actor at the least reliable moment. v3 splits the job:

* **mechanical** — collected from git and the database. No model judgement, so
  it cannot fail for want of context. Always available.
* **semantic** — the model's account of decisions, assumptions and dead ends.
  Valuable and allowed to fail.

The property that matters is in `recover()`: it always produces a workable brief.
The semantic layer improves quality; it is never required for correctness.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import audit, db, repo
from .config import ProjectConfig, load_project

SEMANTIC_FIELDS = (
    "goal", "completed", "remaining", "decisions", "assumptions",
    "blockers", "next_action", "important_files",
)


def mechanical_snapshot(
    conn: sqlite3.Connection,
    root: str | Path,
    worktree: str | Path,
    task_id: int,
    *,
    reason: str = "auto",
    project: ProjectConfig | None = None,
) -> dict[str, Any]:
    """Everything recoverable without asking the model anything."""
    task = db.get_task(conn, task_id) or {}
    project = project or load_project(root)
    base = str(task.get("base_sha") or "")
    head = repo.head_commit(worktree)

    commits: list[str] = []
    if base:
        commits = repo.commits_since(worktree, base)

    lease_rows = [
        {"id": lease["id"], "path_glob": lease["path_glob"], "mode": lease["mode"]}
        for lease in db.active_leases(conn)
        if int(lease["task_id"]) == task_id
    ]

    gate_rows = [
        {"level": row["level"], "passed": bool(row["passed"]),
         "head_sha": row["head_sha"], "summary": str(row["summary"])[:1500]}
        for row in conn.execute(
            "SELECT * FROM gate_results WHERE task_id = ? ORDER BY id DESC LIMIT 5", (task_id,)
        ).fetchall()
    ]

    from .worktree_digest import fingerprint
    audit_result = audit.audit_worktree(conn, project, worktree, task_id, record=False)

    return {
        "kind": "mechanical",
        "reason": reason,
        "task": task_id,
        "generation": int(task.get("generation") or 0),
        "branch": repo.current_branch(worktree) or task.get("branch"),
        "worktree": str(worktree),
        "base_sha": base,
        "head_sha": head,
        "commits_since_start": commits,
        "dirty_digest": fingerprint(worktree),
        "dirty_files": repo.changed_files(worktree),
        "staged_files": repo.staged_files(worktree),
        "lease": lease_rows,
        "gates_run": gate_rows,
        "audit": audit_result.to_dict(),
        "dependencies": {
            "blocked_by": list(task.get("depends_on") or []),
            "blocks": _dependents(conn, task),
        },
        "budget": {
            "spent_usd": float(task.get("spend_usd") or 0.0),
            "cap_usd": task.get("budget_usd"),
        },
        "attempts": int(task.get("attempts") or 0),
        "blocker": task.get("blocker"),
    }


def _dependents(conn: sqlite3.Connection, task: dict[str, Any]) -> list[int]:
    spec_id = task.get("spec_id")
    identifiers = {str(task.get("id"))}
    if spec_id:
        identifiers.add(str(spec_id))
    blocks: list[int] = []
    for other in db.list_tasks(conn):
        deps = {str(d) for d in (other.get("depends_on") or [])}
        if deps & identifiers:
            blocks.append(int(other["id"]))
    return blocks


def write_mechanical(
    conn: sqlite3.Connection,
    root: str | Path,
    worktree: str | Path,
    task_id: int,
    reason: str = "auto",
) -> int:
    snapshot = mechanical_snapshot(conn, root, worktree, task_id, reason=reason)
    return db.write_checkpoint(
        conn, task_id, snapshot, kind="mechanical", reason=reason,
        head_sha=str(snapshot.get("head_sha") or ""),
        generation=int(snapshot.get("generation") or 0),
    )


def write_semantic(
    conn: sqlite3.Connection, task_id: int, payload: dict[str, Any], head_sha: str = ""
) -> int:
    cleaned = {field: payload.get(field) for field in SEMANTIC_FIELDS if field in payload}
    cleaned["kind"] = "semantic"
    task = db.get_task(conn, task_id) or {}
    return db.write_checkpoint(
        conn, task_id, cleaned, kind="semantic", reason="agent",
        head_sha=head_sha, generation=int(task.get("generation") or 0),
    )


# ------------------------------------------------------------------- recovery


def recover(
    conn: sqlite3.Connection,
    root: str | Path,
    worktree: str | Path,
    task_id: int,
) -> dict[str, Any]:
    """The §9.3 sequence. Always returns a usable continuation packet.

    Returns a dict with `mechanical`, `semantic` (possibly reconstructed),
    `reconstructed` and `warnings`.
    """
    warnings: list[str] = []
    project = load_project(root)

    # 1. Mechanical state, rebuilt from git if no checkpoint survived.
    stored = db.latest_checkpoint(conn, task_id, kind="mechanical")
    mechanical = (stored or {}).get("payload") or {}
    if not mechanical:
        warnings.append("no mechanical checkpoint; rebuilt from git and the task record")
        mechanical = mechanical_snapshot(
            conn, root, worktree, task_id, reason="recovery", project=project
        )

    # 2. Trust the worktree only as far as git agrees with it.
    live_head = repo.head_commit(worktree)
    if mechanical.get("head_sha") and live_head and mechanical["head_sha"] != live_head:
        warnings.append(
            f"worktree drift: checkpoint recorded {mechanical['head_sha']}, "
            f"the worktree is at {live_head}. Git wins."
        )
        mechanical = mechanical_snapshot(
            conn, root, worktree, task_id, reason="recovery", project=project
        )

    # 3. Semantic state, only if it describes the commit we are actually on.
    semantic_row = db.latest_checkpoint(conn, task_id, kind="semantic")
    semantic = (semantic_row or {}).get("payload") or {}
    stale_semantic = bool(
        semantic and semantic_row and semantic_row.get("head_sha")
        and semantic_row["head_sha"] != mechanical.get("head_sha")
    )
    reconstructed = False

    if stale_semantic:
        warnings.append(
            "semantic checkpoint is from an earlier commit: keeping its decisions and "
            "assumptions, discarding its progress claims as unreliable"
        )
        semantic = {
            "decisions": semantic.get("decisions") or [],
            "assumptions": semantic.get("assumptions") or [],
        }

    # 4. No usable semantic state: reconstruct from mechanical facts.
    if not semantic.get("next_action"):
        reconstructed = True
        semantic = _reconstruct(conn, task_id, mechanical, semantic)

    # The coordinator may have repaired a blocker after this session stopped.
    # Keep its history, but deliver the current persisted retry instruction.
    current = db.get_task(conn, task_id) or {}
    if current.get("next_action"):
        semantic = {**semantic, "next_action": current["next_action"]}

    return {
        "task": task_id,
        "mechanical": mechanical,
        "semantic": semantic,
        "reconstructed": reconstructed,
        "warnings": warnings,
    }


def _latest_gates(records):
    """Gate rows arrive newest first; previous runs are historical evidence."""
    seen = set()
    latest = []
    for gate in records:
        level = gate.get("level")
        if level not in seen:
            seen.add(level)
            latest.append(gate)
    return latest


def _reconstruct(
    conn: sqlite3.Connection, task_id: int, mechanical: dict[str, Any],
    partial: dict[str, Any],
) -> dict[str, Any]:
    """Build a workable continuation packet from mechanical facts alone."""
    task = db.get_task(conn, task_id) or {}
    commits = list(mechanical.get("commits_since_start") or [])
    dirty = list(mechanical.get("dirty_files") or [])

    blockers: list[str] = list(partial.get("blockers") or [])
    for gate in _latest_gates(mechanical.get("gates_run") or []):
        if not gate.get("passed"):
            blockers.append(f"gate `{gate['level']}` last failed: {gate['summary'][:200]}")
    for violation in (mechanical.get("audit") or {}).get("violations", []):
        blockers.append(f"out-of-lease change pending revert: {violation['path']}")

    if dirty:
        next_action = (
            f"Review the {len(dirty)} uncommitted file(s) in the worktree "
            f"({', '.join(dirty[:3])}{'…' if len(dirty) > 3 else ''}), decide whether to keep "
            "or revert them, then continue the task."
        )
    elif commits:
        next_action = (
            f"Read `git diff {mechanical.get('base_sha', 'HEAD~1')}..HEAD` to see the "
            f"{len(commits)} commit(s) already made, then continue from there."
        )
    else:
        next_action = "No work has been committed yet. Start from the task description."

    return {
        "goal": task.get("description") or task.get("title") or "",
        "completed": commits,
        "remaining": ["derive from the task description and the diff so far"],
        "decisions": list(partial.get("decisions") or []),
        "assumptions": list(partial.get("assumptions") or []),
        "blockers": blockers,
        "next_action": next_action,
        "important_files": dirty[:10],
    }


def render_recovery(packet: dict[str, Any]) -> str:
    """Human/model-readable continuation notes appended to the brief."""
    mechanical = packet["mechanical"]
    semantic = packet["semantic"]
    lines = ["## Continuation state"]

    if packet["reconstructed"]:
        lines.append(
            "_Reconstructed from git and task state — the previous session left no usable "
            "summary. Verify against the diff before trusting it._"
        )
    for warning in packet.get("warnings") or []:
        lines.append(f"- ⚠ {warning}")

    lines.append("")
    lines.append(f"**Branch:** {mechanical.get('branch') or '-'}  "
                 f"**HEAD:** {mechanical.get('head_sha') or '-'}  "
                 f"**Base:** {mechanical.get('base_sha') or '-'}")

    commits = mechanical.get("commits_since_start") or []
    if commits:
        lines += ["", f"**Commits so far ({len(commits)}):**"]
        lines += [f"- {c}" for c in commits[:10]]

    dirty = mechanical.get("dirty_files") or []
    if dirty:
        lines += ["", f"**Uncommitted ({len(dirty)}):** " + ", ".join(f"`{d}`" for d in dirty[:8])]

    gates_run = _latest_gates(mechanical.get("gates_run") or [])
    if gates_run:
        lines += ["", "**Checks recorded:**"]
        lines += [f"- {g.get('level')} {'PASS' if g.get('passed') else 'FAIL'} at "
                  f"{str(g.get('head_sha') or '-')[:12]}" for g in gates_run[:5]]

    for field in ("decisions", "assumptions", "blockers"):
        values = semantic.get(field) or []
        if values:
            lines += ["", f"**{field.title()}:**"]
            lines += [f"- {v}" for v in values]

    if semantic.get("next_action"):
        lines += ["", f"**Continue from:** {semantic['next_action']}"]
    lines += ["", "Do not redo completed work."]
    return "\n".join(lines)
