"""The ownership decision — the one function that makes parallel agents safe.

`decide()` is called from two places that must never disagree:

  * the `PreToolUse` hook, before Claude Code is allowed to write a file;
  * the `lease_check` MCP tool, when a Codex worker asks first or when the
    integrator audits a finished diff before merging it.

Design rule: an unscoped task is *permissive* and a scoped task is *strict*.
A task with no `owned_paths` has not been planned yet and blocking it would make
the framework unusable on day one; a task with `owned_paths` has been planned,
so anything outside them is a planning error worth surfacing loudly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db, globs
from .config import ProjectConfig
from .paths import is_managed

# Paths AgentKit protects in every project, whether or not project.yaml lists them.
ALWAYS_PROTECTED = (
    ".ai/tasks.db",
    ".ai/tasks.db-wal",
    ".ai/tasks.db-shm",
)

CLAIM_STATES = (*db.ACTIVE_STATES, "BLOCKED", "STALE", "NEEDS_REPLAN", "FAILED")


#: Human-held leases. A real task, so it is conflict-checked and logged like any other.
OPERATOR_KIND = "OPERATOR"


def _operator_claim(conn: sqlite3.Connection, rel_path: str) -> dict[str, Any] | None:
    for lease in db.active_leases(conn):
        task = db.get_task(conn, int(lease["task_id"]))
        if not task or str(task.get("kind")) != OPERATOR_KIND:
            continue
        if globs.matches(str(lease["path_glob"]), rel_path):
            return dict(lease)
    return None


def _any_claim(conn: sqlite3.Connection, rel_path: str) -> dict[str, Any] | None:
    for lease in db.active_leases(conn):
        if globs.matches(str(lease["path_glob"]), rel_path):
            return dict(lease)
    for task in db.list_tasks(conn, CLAIM_STATES):
        if task["kind"] == OPERATOR_KIND:
            continue  # The operator's authority is its active lease, checked first.
        for owned in task.get("owned_paths") or []:
            if globs.matches(str(owned), rel_path):
                return {
                    "task_id": int(task["id"]),
                    "task_title": task["title"],
                    "path_glob": str(owned),
                }
    return None


def _foreign_claims(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    """Every path claim held by a live task other than this one.

    Explicit leases come first so their (richer) provenance wins when the same
    glob is claimed twice.
    """
    claims: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    for lease in db.active_leases(conn):
        owner = int(lease["task_id"])
        if owner == task_id or str(lease.get("mode")) != "exclusive-write":
            continue
        key = (owner, str(lease["path_glob"]))
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "task_id": owner,
                "task_title": lease["task_title"],
                "path_glob": str(lease["path_glob"]),
                "source": "lease",
            }
        )

    for task in db.list_tasks(conn, CLAIM_STATES):
        owner = int(task["id"])
        if owner == task_id:
            continue
        for glob in task.get("owned_paths") or []:
            key = (owner, str(glob))
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                {
                    "task_id": owner,
                    "task_title": task["title"],
                    "path_glob": str(glob),
                    "source": "owned_paths",
                }
            )
    return claims


@dataclass
class Decision:
    allowed: bool
    reason: str
    code: str
    owner_task: int | None = None
    matched: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "code": self.code,
            "owner_task": self.owner_task,
            "matched": self.matched,
        }


def decide(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    rel_path: str,
    task_id: int | None,
) -> Decision:
    """Decide whether `task_id` may write `rel_path` (relative to the repo root)."""
    if not rel_path:
        return Decision(True, "No path to check.", "no_path")

    if globs.matches_any(list(ALWAYS_PROTECTED), rel_path):
        return Decision(
            False,
            "`.ai/tasks.db` is AgentKit state. Change it through the agentkit MCP tools "
            "(task_status, checkpoint, lease_request), never by editing the file.",
            "protected_state",
        )

    # No task context. In an unmanaged repository AgentKit stays out of the way
    # entirely. In a *managed* one, a session without a task is a session that
    # can ignore every active lease, so it is denied by default and must take an
    # explicit operator lease instead (§3). The escape hatch is the same lease
    # machinery, not a parallel authorisation path for humans.
    if task_id is None:
        if not is_managed(project.root):
            return Decision(
                True, "Repository is not managed by AgentKit; ownership not enforced.",
                "unmanaged_repository",
            )
        operator = _operator_claim(conn, rel_path)
        if operator is not None:
            return Decision(
                True,
                f"Allowed by operator lease on `{operator['path_glob']}` "
                f"(task {operator['task_id']}).",
                "operator_lease",
                owner_task=int(operator["task_id"]),
                matched=str(operator["path_glob"]),
            )
        blocker = _any_claim(conn, rel_path)
        if blocker is not None:
            return Decision(
                False,
                f"`{rel_path}` is leased by task {blocker['task_id']} "
                f"({blocker['task_title']}), and this session has no AgentKit task. "
                "An agent may be editing it right now. Run "
                "`agentkit operator acquire <paths>` to take an explicit lease, or "
                "resolve that task's lease first.",
                "unmanaged_session_blocked",
                owner_task=int(blocker["task_id"]),
                matched=str(blocker["path_glob"]),
            )
        return Decision(
            False,
            f"This repository is managed by AgentKit and this session has no task, so "
            f"writes to `{rel_path}` are refused. Run `agentkit operator acquire "
            f"{rel_path}` to claim it explicitly (visible in the event log and "
            "conflict-checked against running workers), then edit freely.",
            "unmanaged_session",
        )

    task = db.get_task(conn, task_id)
    if task is None:
        return Decision(True, f"Task {task_id} not found; ownership not enforced.", "unknown_task")

    owned: list[str] = list(task.get("owned_paths") or [])
    if task.get("kind") in ("RESEARCH", "REVIEW"):
        return Decision(False, "Research tasks are read-only; report through checkpoints.", "read_only")
    if globs.matches_any(project.contracts, rel_path) and task.get("kind") != "CONTRACT_CHANGE":
        return Decision(False, "Shared contract files require a CONTRACT_CHANGE task.", "contract_read_only")

    # 1. Another agent's claim always wins, scoped or not.
    #
    # Two things count as a claim: an explicit lease row (dynamic, expiring) and
    # the owned_paths of any task that is currently live. Checking only leases
    # would leave a planned-but-unleased task unprotected, which is the state
    # most tasks are in right after planning.
    for claim in _foreign_claims(conn, task_id):
        if globs.matches(claim["path_glob"], rel_path):
            return Decision(
                False,
                f"`{rel_path}` belongs to task {claim['task_id']} "
                f"({claim['task_title']}) via `{claim['path_glob']}`. "
                "Do not edit it — that task's agent may be in this file right now. "
                "Use lease_request if you genuinely need it, or graph_amend to propose "
                "splitting the work differently.",
                "owned_by_other",
                owner_task=int(claim["task_id"]),
                matched=claim["path_glob"],
            )

    # 2. Within your own declared scope: allowed.
    own_match = globs.matches_any(owned, rel_path)
    if own_match:
        return Decision(True, f"Owned via `{own_match}`.", "owned", task_id, own_match)

    # 3. Hotspots and contracts need an explicit lease even when unscoped.
    protected_match = globs.matches_any(project.protected, rel_path)
    if protected_match:
        kind = "contract" if globs.matches_any(project.contracts, rel_path) else "hotspot"
        return Decision(
            False,
            f"`{rel_path}` is a protected {kind} (`{protected_match}` in .ai/project.yaml) "
            f"and is not in task {task_id}'s owned_paths. Changing it affects other tasks, "
            "so it needs an explicit lease: call lease_request with a reason, or graph_amend "
            "if the task graph is wrong.",
            "protected_path",
            matched=protected_match,
        )

    # 4. Scoped task, path outside scope: block. Unscoped task: allow.
    if owned:
        return Decision(
            False,
            f"`{rel_path}` is outside task {task_id}'s owned_paths ({', '.join(owned)}). "
            "Staying in scope is what lets other agents run at the same time. "
            "Call lease_request to widen scope, or graph_amend to propose a new task.",
            "out_of_scope",
        )

    return Decision(
        True,
        f"Task {task_id} has no owned_paths yet; allowing (declare scope to enable enforcement).",
        "unscoped_task",
    )


def conflicts(
    conn: sqlite3.Connection, candidate_globs: list[str], task_id: int | None = None
) -> list[dict[str, Any]]:
    """Which live claims would collide with these patterns.

    Called before handing two tasks to two agents. Uses the same claim set as
    `decide()`, so a plan that passes this check cannot then be blocked at edit time.
    """
    found: list[dict[str, Any]] = []
    for claim in _foreign_claims(conn, task_id if task_id is not None else -1):
        for pattern in candidate_globs:
            if globs.overlaps(claim["path_glob"], pattern):
                found.append(
                    {
                        "task_id": claim["task_id"],
                        "task_title": claim["task_title"],
                        "held": claim["path_glob"],
                        "requested": pattern,
                        "source": claim["source"],
                    }
                )
                break
    return found
