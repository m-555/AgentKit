"""L7 in practice — combining reviewed work behind the gate that cannot be bypassed.

Two individually green branches can be jointly broken. Only the integration
branch can discover that, which is why the combined suite runs *here* and why a
per-branch pass is never sufficient (PLAN_V3 §11.2).

Hard limits, enforced in code rather than in prose:

* merges go to the integration branch only; `main` needs a human (invariant 14);
* an out-of-lease diff is rejected before anything is merged (invariant 2);
* a semantic conflict stops the merge and reports the pair — it is a planning
  failure, and resolving it here would hide it (§13, failure class 11).
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, contracts, db, repo, reviews, verification, workspace_registry
from . import statemachine as sm
from .config import ProjectConfig
from .locking import exclusive

#: Files never merged textually — regenerated on the integration branch (§11.4).
REGENERATED = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")


@dataclass
class MergeOutcome:
    task_id: int
    ok: bool
    stage: str
    detail: str
    violations: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        mark = "merged " if self.ok else "REJECTED"
        return f"{mark} task {self.task_id} [{self.stage}] {self.detail}"


def integration_branch(project: ProjectConfig) -> str:
    configured = (project.raw or {}).get("integration_branch")
    if configured:
        branch = str(configured)
        protected = {"main", "master", *project.raw.get("protected_branches", [])}
        if branch in protected or branch.startswith("-") or branch.startswith("refs/"):
            raise ValueError(f"integration target {branch!r} is protected or invalid")
        check = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True)
        if check.returncode:
            raise ValueError("invalid integration branch")
        return branch
    for candidate in ("integration", "develop"):
        if repo.branch_exists(project.root, candidate):
            return candidate
    return "integration"


def ensure_integration_branch(project: ProjectConfig) -> str:
    branch = integration_branch(project)
    if not repo.branch_exists(project.root, branch):
        subprocess.run(
            ["git", "branch", branch], cwd=str(project.root), capture_output=True, timeout=60, check=True
        )
    return branch


def queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Widest lease first, so conflicts surface while the queue is short (§11.5)."""
    ready = db.list_tasks(conn, (sm.INTEGRATION_READY,))
    ready.extend(t for t in db.list_tasks(conn, (sm.INTEGRATING,)) if t.get("blocker") in ("manager recovery pending", "interrupted integration"))
    return sorted(ready, key=lambda t: (-len(t.get("owned_paths") or []), int(t["id"])))


def verify(
    conn: sqlite3.Connection, project: ProjectConfig, task: dict[str, Any]
) -> MergeOutcome:
    """Preconditions. Refuse loudly rather than merging hopefully.

    The identity checks below (§8) exist because the audited range is only
    meaningful if it still describes this task's work. A worker that force-resets
    behind its base, squashes away the offending commit, or ends up on another
    task's branch would otherwise present a diff that looks clean because the
    evidence is gone. Every one of these is decided by git, not by a model.
    """
    task_id = int(task["id"])
    worktree = task.get("worktree") or project.root
    branch = task.get("branch")
    if not branch:
        return MergeOutcome(task_id, False, "precondition", "task has no branch")
    if not repo.is_clean(worktree):
        dirty = repo.changed_files(worktree)
        return MergeOutcome(
            task_id, False, "precondition",
            f"worktree has {len(dirty)} uncommitted file(s); commit or revert first",
        )

    identity = _verify_identity(conn, project, task, str(branch), worktree)
    if identity is not None:
        return identity

    target = ensure_integration_branch(project)
    # The task's recorded base is preferred over a freshly computed merge-base:
    # the lease was granted against that commit, and a merge-base computed now can
    # have drifted forward past the very changes the audit exists to inspect.
    base = str(task.get("base_sha") or "") or repo.merge_base(project.root, str(branch), target)
    if not base:
        return MergeOutcome(task_id, False, "precondition",
                            "cannot determine a base commit for the audit")

    result = audit.audit_branch(conn, project, worktree, task_id, base)
    if not result.clean:
        return MergeOutcome(
            task_id, False, "L7_audit",
            f"{len(result.violations)} out-of-lease change(s) in the branch diff",
            [v.to_dict() for v in result.violations],
        )
    return MergeOutcome(task_id, True, "precondition", f"clean against {target} at {base[:8]}")


def _verify_identity(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    task: dict[str, Any],
    branch: str,
    worktree: str | Path,
) -> MergeOutcome | None:
    """§8 — the audited range must still describe this task. None means all good."""
    task_id = int(task["id"])
    base = str(task.get("base_sha") or "")

    def refuse(detail: str) -> MergeOutcome:
        db.log_event(
            conn, task_id, "integration_refused",
            cause=detail, effect="branch not merged; base_sha invariant violated",
            detail={"branch": branch, "base_sha": base},
        )
        return MergeOutcome(task_id, False, "base_sha_invariant", detail)

    if not repo.branch_exists(project.root, branch):
        return refuse(f"recorded branch `{branch}` no longer exists")

    if base:
        if not repo.commit_exists(project.root, base):
            return refuse(
                f"recorded base commit {base[:8]} is gone from the repository; "
                "history was rewritten and the audited range cannot be reconstructed"
            )
        if not repo.is_ancestor(project.root, base, branch):
            return refuse(
                f"base commit {base[:8]} is no longer an ancestor of `{branch}` — the "
                "branch was reset or rewritten behind the commit its lease was granted "
                "against, so the audit would inspect the wrong range"
            )

    # The branch this task was given must be the branch its worktree is on.
    actual = repo.current_branch(worktree)
    if actual and actual != branch:
        return refuse(
            f"worktree {worktree} is on `{actual}` but the task records `{branch}`"
        )

    # And that branch must not belong to another live task.
    for other in db.list_tasks(conn):
        if int(other["id"]) == task_id:
            continue
        if (
            other.get("branch") and str(other["branch"]) == branch
            and str(other["status"]) not in ("DONE", "CANCELLED")
        ):
            return refuse(f"branch `{branch}` is also recorded for task {other['id']}")
    return None


def merge_one(
    conn: sqlite3.Connection,
    project: ProjectConfig,
    task: dict[str, Any],
    *,
    run_full_gate: bool = True,
) -> MergeOutcome:
    from . import manager_state
    manager_state.capture_current(conn)
    if manager_state.pending(conn, task.get("job_id")):
        return MergeOutcome(int(task["id"]), False, "manager_recovery", "current recovery epoch requires manager audit and acknowledgement")
    with exclusive(project.root, "integration"):
        manager_state.capture_current(conn)
        if manager_state.pending(conn, task.get("job_id")):
            return MergeOutcome(int(task["id"]), False, "manager_recovery", "recovery audit became required while waiting for integration lock")
        return workspace_registry.track_merge(project, task,
            lambda: _merge_one(conn, project, task, run_full_gate=run_full_gate),
            lambda: integration_branch(project))


def integration_worktree(project: ProjectConfig) -> Path:
    branch = ensure_integration_branch(project)
    for entry in repo.worktree_list(project.root):
        if entry.get("branch") == f"refs/heads/{branch}":
            target = Path(entry["worktree"])
            if target.resolve() == project.root.resolve():
                raise ValueError("integration branch is checked out in the operator checkout; switch it to another branch")
            return target
    from .worktree_storage import directory
    target = directory(project.root, project) / "integration"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(target), branch], cwd=project.root,
                   capture_output=True, text=True, check=True)
    return target


def _merge_one(conn, project, task, *, run_full_gate=True) -> MergeOutcome:
    task_id = int(task["id"])
    branch = str(task.get("branch") or "")
    try:
        if not run_full_gate:
            raise ValueError("integration always requires the full gate")
        if task["status"] != sm.INTEGRATION_READY and not (task["status"] == sm.INTEGRATING and task.get("blocker") in ("manager recovery pending", "interrupted integration")):
            raise ValueError("task is not INTEGRATION_READY")
        target = ensure_integration_branch(project)
        pre = verify(conn, project, task)
        if pre.ok:
            approved_head = reviews.require_approval(conn, task, project)
        work = integration_worktree(project)
        if not repo.is_clean(work):
            raise ValueError("integration worktree is dirty; preserved for inspection")
        if not project.gate("full"):
            raise ValueError("full gate is not configured")
    except (ValueError, subprocess.SubprocessError) as exc:
        pre = MergeOutcome(task_id, False, "precondition", str(exc))
    if not pre.ok:
        _fail(conn, task_id, pre)
        return pre

    original_head = repo.head_commit(work)
    if task["kind"] != "CONTRACT_CHANGE":
        changed = contracts.verify_unchanged(task["worktree"], project)
        from .contract_versions import compatible
        if changed or not compatible(task, project, work):
            outcome = MergeOutcome(task_id, False, "contract", "frozen contract changed or version is stale")
            _fail(conn, task_id, outcome)
            return outcome

    # Idempotency (§14): already contained is a no-op, not an error.
    if repo.is_ancestor(project.root, approved_head, target):
        from .environment_prepare import prepare
        setup = prepare(project, work, level="full")
        gate = verification.run(conn, project, work, "full") if setup.passed else setup
        if not gate.passed or not repo.is_clean(work) or repo.head_commit(work) != original_head:
            outcome = MergeOutcome(task_id, False, "combined_gate", gate.summary() + "; gate must leave committed tree unchanged")
            _fail(conn, task_id, outcome)
            return outcome
        if task["kind"] == "CONTRACT_CHANGE":
            lock = contracts.load_lock(work)
            if not lock or lock.owner_task != str(task.get("spec_id") or task_id):
                outcome = MergeOutcome(task_id, False, "contract", "contained contract branch has no matching frozen version")
                _fail(conn, task_id, outcome)
                return outcome
            contracts.record_lock(conn, task_id, lock)
        from . import manager_state
        with db.immediate_transaction(conn):
            manager_state.capture_current(conn)
            if manager_state.pending(conn, task.get("job_id")):
                db.update_task(conn, task_id, blocker="manager recovery pending")
                return MergeOutcome(task_id, False, "manager_recovery", "combined gate passed; current epoch awaits manager audit")
        # Still routed through INTEGRATING so the lifecycle has no special cases —
        # a task never reaches DONE by a path the state machine has not seen.
        with db.immediate_transaction(conn):
            manager_state.require_clear(conn, task.get("job_id"))
            db.set_status(conn, task_id, sm.INTEGRATING, actor="integrator", cause="branch already contained in integration")
            db.set_status(conn, task_id, sm.DONE, actor="integrator", cause="nothing to merge")
            db.update_task(conn, task_id, blocker=None)
        db.release_leases(conn, task_id, reason="merged")
        db.refresh_ready(conn)
        return MergeOutcome(task_id, True, "merge", "already merged; nothing to do")

    from . import manager_state
    with db.immediate_transaction(conn):
        manager_state.capture_current(conn)
        if manager_state.pending(conn, task.get("job_id")):
            return MergeOutcome(task_id, False, "manager_recovery", "manager audit required before merge")
        db.set_status(conn, task_id, sm.INTEGRATING, actor="integrator", cause="merge started")
    # Git runs after the commit: the integration lock already serialises merges,
    # and completion re-checks the recovery barrier in its own transaction.
    merged = _git(work, ["merge", "--no-ff", "--no-edit", approved_head])
    if merged.returncode != 0:
        conflicts = _conflicted_files(work)
        _git_abort(work)
        outcome = MergeOutcome(
            task_id, False, "merge_conflict",
            "conflict in " + ", ".join(conflicts[:5]) if conflicts else "merge failed",
            [{"path": p, "reason": "conflict", "code": "conflict"} for p in conflicts],
        )
        db.log_event(
            conn, task_id, "merge_rejected", cause="merge conflict",
            effect="merge aborted; integration branch unchanged",
            detail={"conflicts": conflicts},
        )
        _fail(conn, task_id, outcome)
        return outcome

    frozen = None
    if task["kind"] == "CONTRACT_CHANGE":
        try:
            frozen = contracts.freeze(conn, work, project, task_id, record=False)
            added = _git(work, ["add", ".ai/contracts.lock"])
            if added.returncode:
                raise ValueError(added.stderr)
            recorded = _git(work, ["-c", "core.hooksPath=", "commit", "-m", "Record approved contract version"])
            if recorded.returncode:
                raise ValueError(recorded.stderr)
        except (ValueError, OSError) as exc:
            reset = _git(work, ["reset", "--hard", original_head])
            outcome = MergeOutcome(task_id, False, "contract", f"cannot commit frozen contract: {exc}; rollback exit {reset.returncode}")
            _fail(conn, task_id, outcome)
            return outcome

    if run_full_gate:
        merged_head = repo.head_commit(work)
        from .environment_prepare import prepare
        setup = prepare(project, work, level="full")
        result = verification.run(conn, project, work, "full") if setup.passed else setup
        if not result.passed or not repo.is_clean(work) or repo.head_commit(work) != merged_head:
            reset = _git(work, ["reset", "--hard", original_head])
            outcome = MergeOutcome(
                task_id, False, "combined_gate",
                "combined gate failed or changed committed tree:\n" + result.summary() + f"; rollback exit {reset.returncode}",
            )
            db.log_event(
                conn, task_id, "merge_rejected", cause="combined full gate failed",
                effect="merge reverted; integration branch restored",
                detail={"gate": result.to_dict()},
            )
            _fail(conn, task_id, outcome)
            return outcome
        db.record_gate(conn, task_id, "full", repo.head_commit(work),
                       True, result.summary())

    if frozen is not None:
        contracts.record_lock(conn, task_id, frozen)

    with db.immediate_transaction(conn):
        manager_state.capture_current(conn)
        if manager_state.pending(conn, task.get("job_id")):
            db.update_task(conn, task_id, blocker="manager recovery pending")
            return MergeOutcome(task_id, False, "manager_recovery", "merged result preserved; recovery audit required before completion")
        db.set_status(conn, task_id, sm.DONE, actor="integrator", cause="merged and combined gate passed")
        db.update_task(conn, task_id, blocker=None)
    db.release_leases(conn, task_id, reason="merged")
    promoted = db.refresh_ready(conn)
    db.log_event(
        conn, task_id, "merged", cause=f"into {target}",
        effect=f"unblocked tasks: {promoted or 'none'}",
        detail={"branch": branch, "target": target},
    )
    from .worktree_environments import after_merge
    after_merge(conn, project, task_id)
    return MergeOutcome(task_id, True, "merge",
                        f"merged into {target}; unblocked {promoted or 'nothing'}")


def _fail(conn: sqlite3.Connection, task_id: int, outcome: MergeOutcome) -> None:
    task = db.get_task(conn, task_id)
    if task and sm.can(str(task["status"]), sm.FAILED, "integrator"):
        db.set_status(conn, task_id, sm.FAILED, actor="integrator", cause=outcome.detail[:400])
    db.update_task(conn, task_id, blocker=f"{outcome.stage}: {outcome.detail[:300]}")


def _git(root: str | Path, args: list[str], on_branch: str | None = None):
    if on_branch:
        checkout = subprocess.run(["git", "checkout", on_branch], cwd=str(root),
                                  capture_output=True, text=True, timeout=120)
        if checkout.returncode:
            return checkout
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, timeout=1800)


def _git_abort(root: str | Path) -> None:
    subprocess.run(["git", "merge", "--abort"], cwd=str(root), capture_output=True, timeout=120)


def _conflicted_files(root: str | Path) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(root),
        capture_output=True, text=True, timeout=120,
    )
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def run_queue(
    root: str | Path, *, limit: int = 10, run_full_gate: bool = True
) -> list[MergeOutcome]:
    from .config import load_project

    project = load_project(root)
    conn = db.connect(root)
    outcomes: list[MergeOutcome] = []
    try:
        for task in queue(conn)[:limit]:
            outcome = merge_one(conn, project, task, run_full_gate=run_full_gate)
            outcomes.append(outcome)
            if not outcome.ok and outcome.stage == "combined_gate":
                break        # a broken integration branch stops the queue
        return outcomes
    finally:
        conn.close()
