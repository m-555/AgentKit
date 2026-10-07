"""Explicit integration retry preserves approval and never relaunches a model."""
from . import db, integrator, manager_state, processes, reviews
from .locking import exclusive

#: Task fields the Git verification relied on; a change to any of them makes it stale.
_VERIFIED_FIELDS = ("status", "blocker", "generation", "worktree", "branch", "base_sha", "last_commit")


def _retryable(conn, project, task_id):
    task = db.get_task(conn, task_id)
    blocker = str(task.get("blocker") or "") if task else ""
    contract_retry = blocker == "contract: frozen contract changed or version is stale"
    if not task or task["status"] != "FAILED" or not (blocker.startswith("combined_gate:") or contract_retry):
        raise ValueError("only a failed combined gate or unchanged contract lock may be retried")
    if contract_retry:
        from .contract_versions import compatible
        if (task["kind"] == "CONTRACT_CHANGE" or
                not compatible(task, project, integrator.integration_worktree(project))):
            raise ValueError("frozen contract evidence differs; replan instead of retrying")
    if any(p["task_id"] == task_id for p in processes.owning(conn)):
        raise ValueError("previous worker still owns this task")
    return task


def authorize(conn, project, task_id, evidence, after_tasks=None):
    if not evidence.strip() or len(evidence) > 2000:
        raise ValueError("retry needs 1-2000 characters of remediation evidence")
    with exclusive(project.root, "integration"):
        task = _retryable(conn, project, task_id)
        manager_state.capture_current(conn)
        # Retry authorization repairs a failed audit finding; it never merges or
        # launches a worker. merge_one still requires a successful recovery ack.
        head = reviews.require_approval(conn, task, project)
        # The Git audit runs without the database write lock; the transaction
        # below refuses the retry if anything it relied on changed meanwhile.
        checked = integrator.verify(conn, project, task)
        if not checked.ok:
            raise ValueError(checked.summary())
        with db.immediate_transaction(conn):
            current = db.get_task(conn, task_id)
            if current is None or any(current.get(k) != task.get(k) for k in _VERIFIED_FIELDS):
                raise ValueError("task changed while its retry was being verified; inspect it again")
            if any(p["task_id"] == task_id for p in processes.owning(conn)):
                raise ValueError("previous worker still owns this task")
            if reviews.require_approval(conn, current, project) != head:
                raise ValueError("approval changed while its retry was being verified; inspect it again")
            if after_tasks:
                from .integration_remediation import hold
                hold(conn, current, after_tasks, head, evidence)
                return "Approved commit held for tester remediation; no model or merge launched."
            db.set_status(conn, task_id, "INTEGRATION_READY", actor="integrator", cause=evidence)
            db.update_task(conn, task_id, blocker=None, blocked_meta=None)
            db.log_event(conn, task_id, "integration_retry_authorized", cause=evidence,
                         detail={"head": head, "full_gate_required": True, "model_launch": False})
    return "Approved commit queued for combined full checks; no worker relaunched."
