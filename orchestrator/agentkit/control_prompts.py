"""Control session prompts that fit max_context_chars; a prompt that cannot fit is a problem, never a crash.

A workflow refuses any prompt longer than max_context_chars, and nothing is cut.
Review packets carry a diff, which can be far larger. The diff shrinks until the
whole prompt fits, and the packet says where the rest is. When even the smallest
packet does not fit, the launch is refused and recorded:
- for a final review, the job waits for the user (a "final" problem);
- for a task review, the task does (a "launch" problem);
- for the assigner, the job records the error.
"""
from __future__ import annotations

from . import db, instructions

TAIL = ("\nIf your runtime cannot run shell commands or read files, use the read-only AgentKit tools "
        "source_list, source_read, source_search and source_diff; they serve exact commits. Never "
        "claim to have read code you could not open. In Codex call the tools.mcp__agentkit__* "
        "functions you need directly; never list ALL_TOOLS. Read several files in one call: "
        "source_read accepts paths.")
#: Diff sizes tried in turn; None is the packet's own default, 0 leaves the diff to be read with tools.
SIZES = (None, 8000, 4000, 2000, 0)
REVIEW_REFUSED = "review could not start: "


def fitted(project, task: dict, job: dict, role: str, build) -> str:
    """The prompt with the largest packet `build(size)` that fits."""
    error: ValueError | None = None
    for size in SIZES:
        try:
            return instructions.prompt({**task, "job_id": job["id"]}, project, build(size) + TAIL, role=role)
        except ValueError as exc:
            error = exc
    assert error is not None
    raise error


def refused(conn, job: dict, purpose: str, task: dict, error: str) -> None:
    """Record a control launch that could not be assembled; the next pass does not repeat it."""
    from .job_acceptance import FINAL
    db.log_event(conn, task.get("id"), "control_launch_refused", cause=error[:500],
                 effect="waits for the user", detail={"job": job["id"], "purpose": purpose})
    if purpose == "final_review":
        conn.execute("UPDATE jobs SET status='BLOCKED',last_error=?,updated_at=? WHERE id=? AND status='ACTIVE'",
                     (f"{FINAL}could not start the final review: {error}"[:1500], db.utcnow(), job["id"]))
    elif purpose == "review" and task.get("id"):
        db.update_task(conn, task["id"], blocker=(REVIEW_REFUSED + error)[:1000],
                       next_action="The review prompt could not be assembled; retry after changing the limit.")
    else:
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (error[:1500], job["id"]))
