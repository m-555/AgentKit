"""Evidence-bound recovery audit; observations never clear failed work."""
from __future__ import annotations

import hashlib
import json

from . import (
    audit,
    db,
    globs,
    guard_denials,
    jobs,
    manager_state,
    processes,
    providers,
    repo,
    reviews,
)
from .config import load_project
from .secrets import redact
from .worktree_digest import fingerprint


def _scope_audit(conn, project, work, task):
    result = audit.audit_worktree(conn, project, work, task["id"], record=False).to_dict()
    # Current leases govern new writes, not an unchanged completed commit. Keep
    # its declared scope, clean checkout, exact approval and gate checks intact.
    if (task["status"] == "DONE" and repo.is_clean(work)
            and repo.head_commit(work) == task.get("last_commit")):
        result["violations"] = [item for item in result["violations"]
                                if not (item["code"] == "owned_by_other"
                                        and globs.matches_any(task.get("owned_paths") or [], item["path"]))]
        result["clean"] = not result["violations"]
    return result


def snapshot(conn, root, job_id: str) -> dict:
    project = load_project(root)
    job = jobs.load(root, job_id)
    runtime = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    state = manager_state.state(conn, job_id)
    tasks = []
    findings = []
    for task in db.list_tasks(conn):
        if task.get("job_id") != job_id:
            continue
        task = {k: v for k, v in task.items() if k not in ("heartbeat_at", "updated_at", "spend_usd")}
        identifier = task["id"]
        task["checkpoints"] = [dict(r) for r in conn.execute("SELECT * FROM checkpoints WHERE task_id=? ORDER BY id", (identifier,))]
        task["gates"] = [dict(r) for r in conn.execute("SELECT * FROM gate_results WHERE task_id=? ORDER BY id", (identifier,))]
        task["reviews"] = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id", (identifier,))]
        task["violations"] = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=? ORDER BY id", (identifier,))]
        work = task.get("worktree")
        if task["status"] in ("REVIEW", "INTEGRATION_READY", "INTEGRATING", "DONE") and (
                not work or not task.get("base_sha") or not task.get("branch")):
            findings.append(f"task {identifier}: progressed work is missing worktree/base/branch evidence")
        if work:
            try:
                task["observed_head"] = repo.head_commit(work)
                task["dirty_digest"] = fingerprint(work)
                task["scope_audit"] = _scope_audit(conn, project, work, task)
                task["commits"] = repo.commits_since(work, str(task["base_sha"])) if task.get("base_sha") else []
                # Cancellation quarantines preserved bytes; it does not approve
                # their diff or make the closed lease writable again.
                if not task["scope_audit"]["clean"] and task["status"] != "CANCELLED":
                    findings.append(f"task {identifier}: preserved changes exceed scope")
                if task.get("base_sha") and not repo.is_ancestor(work, task["base_sha"], "HEAD"):
                    findings.append(f"task {identifier}: base ancestry changed")
                if task["status"] in ("INTEGRATION_READY", "INTEGRATING", "DONE"):
                    reviews.require_approval(conn, task, project)
                if task["status"] in ("REVIEW", "INTEGRATION_READY", "INTEGRATING", "DONE"):
                    if not repo.is_clean(work):
                        findings.append(f"task {identifier}: completed/review tree is dirty")
                    gate = db.cached_gate(conn, identifier, task["gate_level"], task["observed_head"])
                    if not gate or not gate["passed"]:
                        findings.append(f"task {identifier}: current commit lacks passing task gate")
            except (OSError, ValueError) as exc:
                findings.append(f"task {identifier}: {exc}")
        if task["status"] == "CANCELLED" and any(
                process["task_id"] == identifier for process in processes.owning(conn)):
            findings.append(f"task {identifier}: cancelled worker ownership is not stopped")
        if (task["violations"] and task["status"] != "CANCELLED"
                and not guard_denials.reviewed(conn, task, task["violations"])):
            findings.append(f"task {identifier}: recorded violations require explicit quarantine/cancellation and corrective task")
        if task["status"] in ("FAILED", "STALE", "NEEDS_REPLAN"):
            from .integration_remediation import addressed
            if not addressed(conn, project, task):
                findings.append(f"task {identifier}: {task['status']} must be addressed explicitly")
        tasks.append(task)
    head = repo.head_commit(root)
    outage_head = state.get("outage_head")
    commits = repo.commits_since(root, outage_head) if outage_head else []
    config = {name: (root / ".ai" / name).read_text(encoding="utf-8")
              for name in ("project.yaml", "tasks.yaml") if (root / ".ai" / name).exists()}
    checkpoints = [dict(r) for r in conn.execute("SELECT * FROM manager_checkpoints WHERE job_id=? ORDER BY id", (job_id,))]
    from .manager_evidence import contracts, integration
    combined = integration(root, state.get("outage_integration_head"))
    for task in tasks:
        if task["status"] == "DONE" and task.get("observed_head") and (
                not combined.get("head") or not repo.is_ancestor(root, task["observed_head"], combined["head"])):
            findings.append(f"task {task['id']}: DONE commit absent from integration branch")
    return {"integration": combined, "contracts": contracts(root), "job": job, "epoch": state["epoch"], "runtime": {k: runtime[k] for k in ("status", "revision", "planned_revision", "completed_sha")},
            "project_head": head, "intervening_commits": commits, "config": config,
            "tasks": tasks, "amendments": db.list_amendments(conn), "manager_checkpoints": checkpoints,
            "findings": findings, "passed": not findings}


def digest(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()


def authority(conn, root, job_id: str, token: str | None = None) -> str:
    job = jobs.load(root, job_id)
    pin = job.get("coordinator_model")
    if not pin:
        raise PermissionError("manager must be pinned before exercising recovery authority")
    if token:
        from .manager import require_lease
        lease = require_lease(conn, job_id, token)
        actual = (lease["provider"], lease["model"], lease["effort"])
        holder = "external:" + lease["holder"]
    else:
        process = processes.require_control(conn, purposes=("coordinator",), job_id=job_id)
        actual = (process["provider"], process.get("requested_model"), process.get("requested_effort"))
        holder = "process:" + str(process["id"])
    if actual != (pin["provider"], pin["model"], pin["effort"]):
        raise PermissionError("recovery authority differs from persisted coordinator pin")
    return holder


def run(conn, root, job_id: str, *, token: str | None = None) -> dict:
    authority(conn, root, job_id, token)
    manager_state.capture_current(conn)
    report = snapshot(conn, root, job_id)
    checksum = digest(report)
    conn.execute("UPDATE manager_state SET audit_epoch=epoch,audit_digest=?,audit_at=?,audit_report=?,updated_at=? WHERE job_id=?",
                 (checksum, db.utcnow(), json.dumps(redact(report), default=str), db.utcnow(), job_id))
    return {"job": job_id, "epoch": report["epoch"], "digest": checksum, "passed": report["passed"],
            "findings": report["findings"], "report": redact(report)}


def _state_marker(conn, job_id: str) -> str:
    """A cheap fingerprint of every database row `snapshot()` reads for this job.

    `acknowledge()` gathers the slow Git and file evidence without the write
    lock, then compares this inside a short transaction: anything recorded in
    between makes the acknowledgement stale.
    """
    tasks = [{k: v for k, v in task.items() if k not in ("heartbeat_at", "updated_at", "spend_usd")}
             for task in db.list_tasks(conn) if task.get("job_id") == job_id]
    ids = [task["id"] for task in tasks]
    within = "(" + ",".join("?" * len(ids)) + ")" if ids else "(NULL)"
    counts = {table: tuple(conn.execute(f"SELECT COUNT(*), MAX(id) FROM {table} WHERE task_id IN {within}", ids).fetchone())
              for table in ("checkpoints", "gate_results", "reviews", "violations")}
    counts["guard_denials_reviewed"] = tuple(conn.execute(
        f"SELECT COUNT(*), MAX(id) FROM events WHERE kind='guard_denials_reviewed' AND task_id IN {within}", ids).fetchone())
    counts["manager_checkpoints"] = tuple(conn.execute(
        "SELECT COUNT(*), MAX(id) FROM manager_checkpoints WHERE job_id=?", (job_id,)).fetchone())
    runtime = conn.execute("SELECT status,revision,planned_revision,completed_sha FROM jobs WHERE id=?", (job_id,)).fetchone()
    state = manager_state.state(conn, job_id)
    return digest({
        "tasks": tasks, "counts": counts, "amendments": db.list_amendments(conn),
        "runtime": dict(runtime) if runtime else None,
        "state": {k: state.get(k) for k in ("epoch", "acknowledged_epoch", "audit_epoch", "audit_digest")},
        "owners": sorted((p["id"], p.get("task_id"), p["purpose"]) for p in processes.owning(conn)),
    })


def _require_current_audit(state: dict, epoch: int, checksum: str) -> None:
    if epoch != state["epoch"] or state.get("audit_epoch") != epoch or checksum != state.get("audit_digest"):
        raise ValueError("stale recovery epoch or audit digest")


def _require_manager_available(conn, root, job_id: str) -> None:
    if not providers.is_available(conn, jobs.load(root, job_id)["coordinator_model"]["provider"]):
        raise ValueError("manager provider has not confirmed availability")


def acknowledge(conn, root, job_id: str, epoch: int, checksum: str, evidence: str, *, token: str | None = None) -> None:
    if not evidence.strip():
        raise ValueError("recovery acknowledgement requires concrete evidence")
    # Cheap refusals first, then the slow Git and file evidence without the write
    # lock: holding it across `snapshot()` starves every other writer.
    authority(conn, root, job_id, token)
    _require_current_audit(manager_state.state(conn, job_id), epoch, checksum)
    _require_manager_available(conn, root, job_id)
    marker = _state_marker(conn, job_id)
    report = snapshot(conn, root, job_id)
    if checksum != digest(report) or not report["passed"]:
        raise ValueError("audit failed or evidence changed; audit again after repairing findings")
    with db.immediate_transaction(conn):
        holder = authority(conn, root, job_id, token)
        _require_current_audit(manager_state.state(conn, job_id), epoch, checksum)
        _require_manager_available(conn, root, job_id)
        if _state_marker(conn, job_id) != marker:
            raise ValueError("evidence changed while it was being verified; audit again")
        conn.execute("UPDATE manager_state SET acknowledged_epoch=?,ack_at=?,ack_by=?,ack_evidence=?,updated_at=? WHERE job_id=?",
                     (epoch, db.utcnow(), holder, str(redact(evidence)), db.utcnow(), job_id))
        db.log_event(conn, None, "manager_recovery_acknowledged", cause=evidence,
                     detail={"job": job_id, "epoch": epoch, "digest": checksum, "holder": holder})
