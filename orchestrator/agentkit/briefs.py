"""Assembling the packet a worker needs to start — or to resume after dying.

This replaces the hand-pasted "you are continuing task #101" prompt. A fresh
session calls `brief()` and gets goal, scope, rules, gate commands and the last
checkpoint, which is everything the previous session knew that still matters.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .config import ProjectConfig


def build(
    conn: sqlite3.Connection, project: ProjectConfig, task_id: int
) -> dict[str, Any] | None:
    task = db.get_task(conn, task_id)
    if task is None:
        return None
    checkpoint = db.latest_checkpoint(conn, task_id)
    semantic = db.latest_checkpoint(conn, task_id, kind="semantic")
    others = [
        {
            "task_id": int(lease["task_id"]),
            "title": lease["task_title"],
            "path_glob": lease["path_glob"],
        }
        for lease in db.active_leases(conn)
        if int(lease["task_id"]) != task_id
    ]
    result = {
        "task": {
            "id": int(task["id"]),
            "title": task["title"],
            "description": task["description"],
            "kind": task["kind"],
            "status": task["status"],
            "role": task["role"],
            "complexity": task.get("complexity", "standard"),
            "model_profile": task.get("model_profile"),
            "model": task.get("model"),
            "branch": task["branch"],
            "worktree": task["worktree"],
            "attempts": task["attempts"],
            "next_action": task["next_action"],
        },
        "owned_paths": task["owned_paths"],
        "depends_on": task["depends_on"],
        "expected_read": task["expected_read"],
        "acceptance": task.get("acceptance", []),
        "skills": task.get("skills", []),
        "job_id": task.get("job_id"),
        "job_memory": _job_memory(project, task),
        "protected_paths": project.protected,
        "gate_level": task["gate_level"],
        "gate_commands": project.gate(str(task["gate_level"] or "fast")),
        "full_gate_commands": project.gate("full"),
        "last_checkpoint": (checkpoint or {}).get("payload"),
        "checkpoint_at": (checkpoint or {}).get("created_at"),
        "last_semantic_checkpoint": semantic,
        "other_active_leases": others,
        "project": {"name": project.name, "stacks": project.stacks},
    }
    from . import test_packets
    result["implementation_handoff"] = test_packets.build(conn, project, task)
    from .workflow import enabled, validate_prompt
    if enabled(project):
        # Exact owned outputs are already readable in sourceview; include them
        # explicitly so an editing session never has to request its own input.
        result["readable_paths"] = list(dict.fromkeys(
            [*task["expected_read"], *task["expected_write"]]))
        result["expected_write"] = task["expected_write"]
        result["full_gate_commands"] = []
        from .brief_checkpoint import scope
        scope(result, db.latest_checkpoint(conn, task_id, kind="mechanical"))
        validate_prompt(project, render(result))
    return result


def render(brief: dict[str, Any]) -> str:
    """Human/model-readable form, injected at session start."""
    task = brief["task"]
    lines = [
        f"# AgentKit brief — task {task['id']}: {task['title']}",
        "",
        f"**Kind:** {task['kind']}   **Status:** {task['status']}   **Role:** {task['role']}",
    ]
    if task.get("branch") or task.get("worktree"):
        lines.append(f"**Branch:** {task.get('branch') or '-'}   **Worktree:** {task.get('worktree') or '-'}")
    if task.get("description"):
        lines += ["", "## Goal", task["description"]]
    for label, key in (("Required inputs", "expected_read"), ("Acceptance criteria", "acceptance"), ("Skills to read", "skills")):
        if brief.get(key):
            lines += ["", f"## {label}", *[f"- {v}" for v in brief[key]]]
    if brief.get("job_memory"):
        lines += ["", brief["job_memory"]]

    from .test_packets import render as render_handoff
    lines += render_handoff(brief.get("implementation_handoff") or [])

    if brief.get("expected_write"):
        lines += ["", "## Deliverables (create missing output files)",
                  *[f"- {path}" for path in brief["expected_write"]]]
    owned = brief.get("owned_paths") or []
    lines += ["", "## Your scope (edits outside this are blocked)"]
    lines += [f"- `{p}`" for p in owned] or [
        "- (not yet scoped — declare owned_paths before editing shared code)"
    ]

    others = brief.get("other_active_leases") or []
    if others:
        lines += ["", "## Held by other agents right now — do not touch"]
        lines += [f"- `{o['path_glob']}` (task {o['task_id']}: {o['title']})" for o in others]

    protected = brief.get("protected_paths") or []
    if protected:
        lines += ["", "## Protected (needs an explicit lease)"]
        lines += [f"- `{p}`" for p in protected]

    gate_cmds = brief.get("gate_commands") or []
    if gate_cmds:
        lines += ["", f"## Gate before you stop (`{brief.get('gate_level')}`)"]
        lines += [f"- `{c}`" for c in gate_cmds]

    semantic = brief.get("last_semantic_checkpoint") or {}
    checkpoint = semantic.get("payload") or brief.get("last_checkpoint")
    if checkpoint:
        lines += ["", f"## Where the last session stopped ({semantic.get('created_at') or brief.get('checkpoint_at')})"]
        if semantic.get("head_sha"):
            lines.append(f"Recorded against commit {semantic['head_sha']}; verify against the current worktree.")
        for key in ("completed", "remaining", "files_changed", "decisions"):
            values = checkpoint.get(key)
            if values:
                lines.append(f"**{key.replace('_', ' ').title()}:**")
                if isinstance(values, list):
                    lines += [f"- {v}" for v in values]
                else:
                    lines.append(f"- {values}")
        if checkpoint.get("next_action") and not task.get("next_action"):
            lines += ["", f"**Continue from:** {checkpoint['next_action']}"]
        lines += ["", "Do not redo completed work."]
    if task.get("next_action"):
        lines += ["", "## Current instruction (takes priority over earlier session notes)",
                  str(task["next_action"])]

    lines += [
        "",
        "## Rules",
        "- Commit small logical milestones; the gate runs against your commits.",
        "- If you need a file outside your scope, call `lease_request` — do not edit it.",
        "- If the task itself is wrong, call `graph_amend` instead of widening it silently.",
        "- Call `checkpoint` before you stop. Hooks also do this automatically.",
    ]
    return "\n".join(lines)


def _job_memory(project, task):
    from . import workflow
    return workflow.job_context(project, task.get("job_id"), task)
