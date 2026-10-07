"""Hold exact stopped tester commits for host refresh, without another model."""
from pathlib import Path

from . import audit, checkpoints, db, host_completion, repo
from .locking import exclusive


def hold(conn, project, task_id: int, source_task_id: int, evidence: str) -> dict:
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
        raise ValueError("Concrete hold evidence must contain 1-2000 characters")
    with exclusive(project.root, f"refresh-tests-{task_id}"):
        return _hold(conn, project, task_id, source_task_id, evidence)


def _hold(conn, project, task_id, source_task_id, evidence):
    with db.immediate_transaction(conn):
        task = db.get_task(conn, task_id)
        if not task or task["kind"] != "TEST_ONLY" or task["status"] != "READY":
            raise ValueError("Only a stopped READY tester with committed tests can be held")
        host_completion.authority(conn, project, task)
        source = db.get_task(conn, source_task_id)
        if (not source or source.get("job_id") != task.get("job_id")
                or source.get("kind") in ("TEST_ONLY", "RESEARCH", "REVIEW", "OPERATOR")
                or source.get("spec_id") not in (task.get("depends_on") or [])
                or source["status"] in ("CANCELLED", "STALE", "NEEDS_REPLAN", "FAILED")):
            raise ValueError("Hold requires its declared same-job source correction")
        work = Path(task.get("worktree") or "").resolve(strict=True)
        head = repo.head_commit(work)
        if (not task.get("worktree") or not repo.is_clean(work)
                or repo.current_branch(work) != task.get("branch")
                or head != task.get("last_commit") or head == task.get("base_sha")
                or not task.get("base_sha") or not repo.is_ancestor(work, task["base_sha"], head)
                or not repo.diff_files(work, task["base_sha"])):
            raise ValueError("Hold requires exact clean recorded committed tests")
        if not audit.audit_worktree(conn, project, work, task_id, record=False).clean:
            raise PermissionError("Preserved tester commit exceeds its scope")
        db.set_status(conn, task_id, "BLOCKED", actor="human", cause=evidence)
        db.update_task(conn, task_id, blocker="Host test refresh awaits accepted source correction",
                       next_action=f"Host test_refresh({task_id}, {source_task_id}); preserve test bytes")
        db.log_event(conn, task_id, "host_test_hold", cause=evidence,
                     detail={"head": head, "source_task": source_task_id, "ai_calls": 0})
    db.release_leases(conn, task_id, reason="Host holds unchanged tester commit")
    checkpoints.write_mechanical(conn, project.root, work, task_id, "host_test_hold")
    return {"task_id": task_id, "head": head, "source_task_id": source_task_id, "held": True}
