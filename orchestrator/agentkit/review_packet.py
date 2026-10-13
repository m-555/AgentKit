"""What an independent reviewer needs about one exact commit, delivered in its prompt.

On the benchmark a review took about ten turns, most of them fetching the brief, the
scope audit and the diff of the commit it was assigned, each re-sent on every later
turn. The packet hands those over once. The reviewer still decides independently,
may read anything else, and approval still re-checks scope and the gate itself.
"""
from __future__ import annotations

from . import audit, db, repo
from .secrets import redact_text

#: Characters of diff included; the rest is paged with task_diff.
LIMIT = 15000


def build(conn, project, task, head: str, *, limit: int | None = None) -> str:
    size = LIMIT if limit is None else limit
    work, base = task.get("worktree"), task.get("base_sha")
    lines = [f"## Review packet: task {task['id']} at commit {head[:12]}",
             f"Goal: {task.get('description') or task['title']}"]
    acceptance = task.get("acceptance") or []
    if acceptance:
        lines += ["Acceptance:", *[f"- {item}" for item in acceptance]]
    result = audit.audit_worktree(conn, project, work, task["id"], record=False)
    lines.append("Ownership scope: clean (every change is inside the task's owned paths)" if result.clean else
                 "Ownership scope: VIOLATIONS - " + "; ".join(f"{v.path}: {v.reason}" for v in result.violations[:10]))
    gate = db.cached_gate(conn, task["id"], task["gate_level"], head)
    if gate:
        verdict = "passed" if gate["passed"] else "FAILED"
        lines.append(f"Gate `{task['gate_level']}` at this commit {verdict}: {str(gate['summary'] or '')[:1500]}")
    else:
        lines.append(f"Gate `{task['gate_level']}`: no recorded run at this commit (a PASS verdict reruns it).")
    if base:
        files = repo.diff_files(work, base)
        lines.append(f"Changed files ({len(files)}): " + ", ".join(files))
        diff = redact_text(repo._git(["diff", "--no-color", f"{base}..{head}"], work))
        if size:
            lines += ["```diff", diff[:size], "```"]
        if len(diff) > size:
            lines.append(f"[The diff is {len(diff):,} characters; the first {size:,} are above. Page the rest "
                         f"with task_diff(offset={size}).]")
    lines.append("This packet is the exact assigned commit: do not fetch it again. Read anything else you need.")
    return "\n".join(lines)
