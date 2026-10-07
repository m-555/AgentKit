"""Continuing preserved work in a new session, possibly on another provider.

A continuation is only safe when the previous worker is provably gone and the
preserved worktree is still exactly the task's own work. Every check below is
decided by process tables, pids and git, never by a model's account:

1. no session owns the task, and the last monitor and its child are confirmed dead;
2. the previous worker run is closed;
3. the worktree is on the task's branch, descends from its recorded base and has
   not moved since the last worker's exit checkpoint;
4. committed and uncommitted changes stay inside the task's scope;
5. no other live task records the same branch or worktree or claims its paths.

A provider's session ID is meaningless to another provider, so a cross-provider
continuation always starts a fresh session with a recovery packet. Earlier review
verdicts are invalidated: the final commit needs a fresh independent review.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, checkpoints, db, processes, repo, reviews
from . import statemachine as sm
from .process_identity import alive
from .reconcile import pid_alive


@dataclass
class Continuation:
    ok: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    cross: bool = False


def _refuse(reason: str, evidence: dict[str, Any]) -> Continuation:
    return Continuation(False, f"continuation refused: {reason}", evidence)


def verify(conn: sqlite3.Connection, project, task: dict[str, Any], worktree: str | Path,
           target) -> Continuation:
    """Prove the previous worker is gone and the preserved work is still this task's."""
    task_id = int(task["id"])
    base = str(task.get("base_sha") or "")
    last = conn.execute("SELECT * FROM processes WHERE task_id=? AND purpose='worker' ORDER BY id DESC LIMIT 1",
                        (task_id,)).fetchone()
    # The source is the session that last ran, not the task record: a failed launch
    # may already have written the next provider into the task.
    source_provider = (last["provider"] if last else None) or task.get("adapter")
    source_model = (_requested_model(last) if last else None) or task.get("model")
    evidence: dict[str, Any] = {"task": task_id, "base_sha": base, "worktree": str(worktree),
                                "source_provider": source_provider, "source_model": source_model,
                                "target_provider": target.provider, "target_model": target.model,
                                "target_effort": target.effort}

    live = [p for p in processes.owning(conn) if p["task_id"] == task_id]
    if live:
        return _refuse(f"session {live[0]['id']} is still {live[0]['status']}", evidence)
    if last is not None:
        evidence["previous_process"] = int(last["id"])
        for label in ("pid", "child_pid"):
            if alive(dict(last), label, probe=pid_alive):
                return _refuse(f"previous {'monitor' if label == 'pid' else 'agent'} pid {last[label]} "
                               "may still be alive", evidence)
    for old in conn.execute("SELECT * FROM worker_runs WHERE task_id=?", (task_id,)):
        if alive(dict(old), probe=pid_alive):
            return _refuse("a prior worker-run PID may still be alive", evidence)
    run = db.latest_worker_run(conn, task_id)
    if run is not None:
        evidence["previous_run"] = int(run["id"])
        if not run.get("ended_at"):
            return _refuse(f"worker run {run['id']} is still open; the supervisor has not recorded its exit",
                           evidence)
        if alive(run, probe=pid_alive):
            return _refuse(f"worker run pid {run['pid']} may still be alive", evidence)

    work = Path(worktree)
    if not work.is_dir():
        return _refuse(f"preserved worktree {work} is missing", evidence)
    head = repo.head_commit(work)
    branch = repo.current_branch(work)
    evidence.update(head_sha=head, branch=branch)
    if task.get("branch") and branch != task["branch"]:
        return _refuse(f"worktree is on {branch!r}, but the task records {task['branch']!r}", evidence)
    if not base or not repo.is_ancestor(work, base, "HEAD"):
        return _refuse("preserved worktree no longer descends from its recorded base", evidence)
    stored = db.latest_checkpoint(conn, task_id, kind="mechanical")
    recorded = str(((stored or {}).get("payload") or {}).get("head_sha") or "")
    evidence["checkpoint_head"] = recorded or None
    if recorded and recorded != head:
        return _refuse(f"HEAD moved from {recorded[:12]} to {head[:12]} after the last worker stopped", evidence)

    result = audit.audit_worktree(conn, project, work, task_id, record=False)
    evidence["audit"] = result.to_dict()
    if not result.clean:
        return _refuse("preserved changes are outside the task scope: " + result.summary(), evidence)
    from .worktree_digest import fingerprint
    payload = (stored or {}).get("payload") or {}
    if not recorded or not payload.get("dirty_digest"):
        # A failed preflight reserves a run but never creates an agent process.
        # It has no worker-exit checkpoint; only a pristine original checkout
        # with exclusively closed, PID-less reservations can retry this way.
        never_started = last is None and run is not None and not task.get("session_token")
        pristine = head == base and repo.is_clean(work)
        reservations = conn.execute("SELECT pid,ended_at FROM worker_runs WHERE task_id=?", (task_id,)).fetchall()
        if not (never_started and pristine and all(r["pid"] is None and r["ended_at"] for r in reservations)):
            return _refuse("exit checkpoint is missing byte evidence; explicit recovery audit required", evidence)
        evidence["never_spawned"] = True
        evidence["dirty_digest"] = fingerprint(work)
    else:
        if payload["dirty_digest"] != fingerprint(work):
            return _refuse("uncommitted bytes or index changed after the exit checkpoint", evidence)
        evidence["dirty_digest"] = payload["dirty_digest"]
    evidence["uncommitted"] = repo.changed_files(work)

    for other in db.list_tasks(conn):
        if int(other["id"]) == task_id or str(other["status"]) in sm.TERMINAL:
            continue
        if (branch and other.get("branch") == branch) or (other.get("worktree") and
                                                           Path(str(other["worktree"])) == work):
            return _refuse(f"task {other['id']} also records this branch or worktree", evidence)
    from .leases import conflicts
    paths = list(task.get("expected_write") or task.get("owned_paths") or [])
    clashes = conflicts(conn, paths, task_id)
    if clashes:
        return _refuse(f"task {clashes[0]['task_id']} claims {clashes[0]['held']}", evidence)

    cross = (source_provider or target.provider) != target.provider or (
        source_model or target.model) != target.model
    return Continuation(True, "previous worker confirmed stopped; scope and ancestry verified", evidence, cross)


def _requested_model(process) -> str | None:
    if process["requested_model"]:
        return str(process["requested_model"])
    try:
        return json.loads(process["launch_json"] or "{}").get("env", {}).get("AGENTKIT_MODEL") or None
    except ValueError:
        return None


def packet(conn: sqlite3.Connection, root: str | Path, worktree: str | Path, task: dict[str, Any],
           continuation: Continuation) -> str:
    """Continuation notes for the next session: decisions, checks, dirt, next action."""
    evidence = continuation.evidence
    lines = []
    if continuation.cross:
        from .policy import trigger
        lines += ["## Cross-provider continuation",
                  f"Previous worker: {evidence.get('source_provider')} / {evidence.get('source_model')}. "
                  f"You are {evidence['target_provider']} / {evidence['target_model']} "
                  f"({evidence.get('target_effort') or 'default'} effort) in a fresh session.",
                  f"Reason: {trigger(conn, int(task['id'])) or 'previous provider unavailable'}. "
                  "The previous session cannot be resumed here; rely on the repository and this packet.",
                  "Keep the existing branch, commits and in-scope uncommitted changes. Do not rewrite history.",
                  ""]
    uncommitted = evidence.get("uncommitted") or []
    if uncommitted:
        stat = repo._git(["diff", "--stat", "HEAD", "--"], worktree)
        lines += ["**Uncommitted change summary:**", "```", stat.strip()[:4000], "```", ""]
    recovery = checkpoints.recover(conn, root, worktree, int(task["id"]))
    from .config import load_project
    from .workflow import enabled
    if enabled(load_project(root)) and db.latest_checkpoint(conn, int(task["id"]), kind="semantic"):
        # The task brief delivers completed/remaining/decisions/current instruction
        # already. Keep the mechanical proof and nonduplicated semantic fields;
        # never shorten job intent or discard evidence from durable storage.
        recovery = {**recovery, "semantic": {key: value for key, value in recovery["semantic"].items()
                                           if key in ("assumptions", "blockers")}}
    lines.append(checkpoints.render_recovery(recovery))
    return "\n".join(lines)


def record(conn: sqlite3.Connection, task: dict[str, Any], continuation: Continuation, generation: int) -> None:
    """Make the move visible and invalidate approvals that described the old session."""
    from .policy import trigger
    evidence = continuation.evidence
    reason = trigger(conn, int(task["id"])) or ""
    conn.execute("INSERT INTO handoffs(task_id,generation,source_provider,source_model,target_provider,"
                 "target_model,trigger,reason,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (task["id"], generation, evidence.get("source_provider") or "", evidence.get("source_model") or "",
                  evidence["target_provider"], evidence["target_model"], reason, continuation.reason,
                  json.dumps(evidence, default=str), db.utcnow()))
    db.log_event(conn, int(task["id"]), "worker_handoff",
                 cause=f"{evidence.get('source_provider')}/{evidence.get('source_model')} -> "
                       f"{evidence['target_provider']}/{evidence['target_model']} ({reason or 'continuation'})",
                 effect="fresh session with recovery packet; earlier reviews invalidated",
                 detail=evidence)
    if conn.execute("SELECT 1 FROM reviews WHERE task_id=?", (task["id"],)).fetchone():
        reviews.invalidate(conn, int(task["id"]), str(evidence.get("head_sha") or ""),
                           "cross-provider continuation; the final commit needs a fresh independent review")


def history(conn: sqlite3.Connection, task_id: int | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM handoffs" + (" WHERE task_id=?" if task_id is not None else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(query, (task_id,) if task_id is not None else ())]
