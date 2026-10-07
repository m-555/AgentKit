"""Bringing runtime state back into agreement with the spec and with git.

The only path that mutates state at startup (PLAN_V3 Â§4.3). Its governing rule
is invariant 6: **recovery never invents authority.** A rebuilt database grants
no leases and advances no task. Where the truth is unknowable â€” is that worker
still alive? â€” the answer is STALE and a human decides, because inventing a lease
is the single mistake that puts two agents in one file.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, repo, spec
from . import statemachine as sm


@dataclass
class ReconcileReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    replan: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    expired_leases: int = 0
    orphans: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.created:
            lines.append(f"created {len(self.created)}: {', '.join(self.created)}")
        if self.updated:
            lines.append(f"updated {len(self.updated)}: {', '.join(self.updated)}")
        if self.replan:
            lines.append(f"NEEDS_REPLAN {len(self.replan)}: {', '.join(self.replan)}")
        if self.stale:
            lines.append(f"STALE {len(self.stale)}: {', '.join(self.stale)}")
        if self.expired_leases:
            lines.append(f"expired {self.expired_leases} lease(s) past their heartbeat")
        if self.promoted:
            lines.append(f"promoted to READY: {', '.join(str(i) for i in self.promoted)}")
        if self.orphans:
            lines.append(f"in the database but not in tasks.yaml: {', '.join(self.orphans)}")
        for note in self.notes:
            lines.append(note)
        return "\n".join(lines) or "nothing to reconcile"


def pid_alive(pid: int | None) -> bool:
    """Best-effort liveness. Wrong answers must be conservative, never optimistic."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                if ctypes.get_last_error() == 87:
                    return False
                from .windows_processes import present
                return present(pid) is not False  # Only a complete snapshot proves absence.
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError):
            return True
    try:
        os.kill(pid, 0)
        # kill(0) also succeeds for zombies, which cannot execute or own work.
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True  # Unknown identity remains conservative.
        return state not in ("Z", "X")
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def reconcile(root: str | Path, *, adopt_running: bool = False) -> ReconcileReport:
    root_path = Path(root)
    report = ReconcileReport()
    specs = spec.load(root_path)
    conn = db.connect(root_path)
    try:
        _sync_specs(conn, specs, report)
        _expire_leases(conn, report)
        _detect_stale_workers(conn, report, adopt_running=adopt_running)
        _find_orphans(conn, specs, report)
        report.promoted = db.refresh_ready(conn)
        db.log_event(conn, None, "reconciled", cause="orchestrator start",
                     detail={"created": len(report.created), "replan": len(report.replan),
                             "stale": len(report.stale)})
    finally:
        conn.close()
    return report


def _sync_specs(
    conn: sqlite3.Connection, specs: list[spec.TaskSpec], report: ReconcileReport
) -> None:
    for task_spec in specs:
        existing = db.get_task_by_spec(conn, task_spec.spec_id)
        spec_hash = task_spec.hash()
        fields: dict[str, Any] = {
            "title": task_spec.title,
            "description": task_spec.description,
            "kind": task_spec.kind,
            "role": task_spec.role,
            "expected_write": task_spec.expected_write,
            "expected_read": task_spec.expected_read,
            "depends_on": task_spec.depends_on,
            "gate_level": task_spec.gate_level,
            "priority": task_spec.priority,
            "contract_version": task_spec.contract_version,
            "budget_usd": task_spec.budget_usd,
            "job_id": task_spec.job_id,
            "skills": task_spec.skills,
            "acceptance": task_spec.acceptance,
            "complexity": task_spec.complexity,
            "model_profile": task_spec.model_profile,
            "model_assignment": task_spec.model_assignment,
            "spec_hash": spec_hash,
        }

        if existing is None:
            db.create_task(
                conn, spec_id=task_spec.spec_id, status=sm.PLANNED,
                owned_paths=task_spec.expected_write, **fields,
            )
            report.created.append(task_spec.spec_id)
            continue

        if str(existing.get("spec_hash") or "") == spec_hash:
            continue

        status = str(existing["status"])
        if status in sm.TERMINAL:
            # Editing the spec of finished work changes nothing that already happened.
            db.update_task(conn, int(existing["id"]), spec_hash=spec_hash)
            report.notes.append(
                f"{task_spec.spec_id}: spec changed after completion; recorded, not re-run"
            )
            continue

        if status in (sm.PLANNED, sm.READY):
            db.update_task(
                conn, int(existing["id"]), owned_paths=task_spec.expected_write, **fields
            )
            report.updated.append(task_spec.spec_id)
            continue

        # In flight and the spec moved underneath it â€” Â§4.3.
        db.update_task(conn, int(existing["id"]), spec_hash=spec_hash,
                       blocker=existing.get("blocker") or "spec changed while task was in flight")
        with suppress(sm.TransitionError):
            db.set_status(conn, int(existing["id"]), sm.NEEDS_REPLAN, actor="scheduler",
                          cause="tasks.yaml edited while this task was in flight")
        # Keep ownership until the supervisor has fenced and stopped the old process.
        # Releasing here would allow another task to write while it is still alive.
        report.replan.append(task_spec.spec_id)


def _expire_leases(conn: sqlite3.Connection, report: ReconcileReport) -> None:
    expired = db.expire_stale_leases(conn)
    report.expired_leases = len(expired)
    for lease in expired:
        db.log_event(
            conn, int(lease["task_id"]), "lease_expired",
            cause=f"no heartbeat since {lease.get('heartbeat_at') or lease.get('acquired_at')}",
            effect="lease released; task will be marked STALE",
            detail={"lease": lease["id"], "path_glob": lease["path_glob"]},
        )


def _detect_stale_workers(
    conn: sqlite3.Connection, report: ReconcileReport, *, adopt_running: bool
) -> None:
    """A task is only still RUNNING if something is demonstrably running it."""
    for task in db.list_tasks(conn, (sm.LEASED, sm.RUNNING, sm.VERIFYING)):
        if task["kind"] == "OPERATOR":
            continue  # A human's claim has no worker process; its lease TTL governs it.
        task_id = int(task["id"])
        run = db.latest_worker_run(conn, task_id)
        pid = run.get("pid") if run else None
        if _pending_spawn(task, run):
            report.notes.append(f"task {task_id}: guarded launch is settling; PID not yet recorded")
            continue
        alive = bool(run and run.get("ended_at") is None and pid_alive(pid))
        has_lease = task["kind"] in ("RESEARCH", "REVIEW") or any(
            int(lease["task_id"]) == task_id for lease in db.active_leases(conn)
        )

        if alive and has_lease and adopt_running:
            db.log_event(conn, task_id, "worker_adopted",
                         cause=f"pid {pid} alive and lease fresh",
                         effect="left RUNNING")
            continue
        if alive and has_lease:
            report.notes.append(
                f"task {task_id}: worker pid {pid} still alive with a fresh lease; "
                "left as-is (pass --adopt to claim it)"
            )
            continue

        cause = (
            "no worker run recorded" if not run
            else f"worker pid {pid} is not running" if not alive
            else "lease expired"
        )
        db.release_leases(conn, task_id, reason=f"stale: {cause}")
        try:
            db.set_status(conn, task_id, sm.STALE, actor="scheduler", cause=cause)
        except sm.TransitionError:
            continue
        db.update_task(conn, task_id, blocker=cause)
        report.stale.append(str(task.get("spec_id") or task_id))


def _find_orphans(
    conn: sqlite3.Connection, specs: list[spec.TaskSpec], report: ReconcileReport
) -> None:
    known = {s.spec_id for s in specs}
    for task in db.list_tasks(conn):
        spec_id = task.get("spec_id")
        if spec_id and spec_id not in known and str(task["status"]) not in sm.TERMINAL:
            report.orphans.append(str(spec_id))


def rebuild_from_git(root: str | Path) -> ReconcileReport:
    """Recover runtime state after the database is lost (Â§4.3, case 1).

    Derives status conservatively from branches and grants no leases: a rebuilt
    database cannot know whether a worker is still running, and guessing is the
    one mistake that produces two agents in one file.
    """
    root_path = Path(root)
    report = reconcile(root_path)
    conn = db.connect(root_path)
    try:
        integration = _integration_branch(root_path)
        for task in db.list_tasks(conn):
            task_id = int(task["id"])
            spec_id = task.get("spec_id") or task_id
            from .worktrees import branch_name
            branch = task.get("branch") or branch_name(task)
            if not repo.branch_exists(root_path, str(branch)):
                continue

            base = repo.merge_base(root_path, str(branch), integration) if integration else ""
            ahead = repo._git(["log", "--oneline", f"{base}..{branch}"], root_path).splitlines() if base else []
            merged = bool(
                integration and repo.is_ancestor(root_path, str(branch), integration)
            )

            target = sm.NEEDS_REPLAN
            cause = "branch exists; review and process ownership must be reconstructed"
            if merged:
                cause += "; branch is contained in integration but approval evidence was lost"
            elif ahead:
                cause += f"; {len(ahead)} unmerged commit(s)"

            matches = [w["worktree"] for w in repo.worktree_list(root_path) if w.get("branch") == f"refs/heads/{branch}"]
            db.update_task(conn, task_id, branch=str(branch), base_sha=base or None,
                           worktree=matches[0] if matches else None)
            current = str(task["status"])
            if current != target and sm.can(current, target, "scheduler"):
                db.set_status(conn, task_id, target, actor="scheduler",
                              cause=f"rebuilt from git: {cause}")
                report.notes.append(f"task {spec_id}: {current} -> {target} ({cause})")
        report.notes.append("no leases were granted; adopt or discard STALE tasks explicitly")
    finally:
        conn.close()
    return report


def _integration_branch(root: str | Path) -> str:
    for candidate in ("integration", "develop", "main", "master"):
        if repo.branch_exists(root, candidate):
            return candidate
    return ""


def _pending_spawn(task, run):
    """Preserve a current launch claim for at most 60s, without claiming it is alive."""
    if run:
        if run.get('ended_at') or run.get('pid') or run.get('generation') != task.get('generation'):
            return False
        stamp = db.parse_ts(run.get('started_at'))
    else:
        if task['status'] != sm.LEASED or not task.get('generation'):
            return False
        stamp = db.parse_ts(task.get('updated_at'))
    return bool(stamp and 0 <= (datetime.now(UTC) - stamp).total_seconds() < 60)
