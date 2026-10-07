"""Host-owned task filesystem preparation, before any generation or model starts."""
from pathlib import Path

from . import db, workflow


def prepare(conn, project, task, work):
    if not workflow.enabled(project):
        return
    work = Path(work).resolve()
    reads = list(task.get("expected_read") or [])
    writes = list(task.get("expected_write") or task.get("owned_paths") or [])
    # Validate every path first: no partial preparation of an invalid assignment.
    for relative in reads + writes:
        path = work / relative
        if not path.resolve().is_relative_to(work):
            raise ValueError("Host preparation blocked: assigned path escapes checkout: " + relative)
    missing = [relative for relative in reads if not (work / relative).is_file()]
    if missing:
        raise ValueError("Host preparation blocked: manager must repair missing task inputs: " + ", ".join(missing))
    for relative in writes:
        target = work / relative
        if target.is_dir():
            raise ValueError("Host preparation blocked: output file is a directory: " + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
    db.log_event(conn, task["id"], "worker_files_prepared", detail={
        "read_files": reads, "write_files": writes, "worktree": str(work), "ai_calls": 0})


def validate_completed(project, task, work):
    """A passing build cannot substitute for missing assigned deliverables."""
    if not workflow.enabled(project):
        return
    work = Path(work).resolve(strict=True)
    missing = []
    for relative in task.get("expected_write") or []:
        path = work / relative
        if not path.resolve().is_relative_to(work) or not path.is_file():
            missing.append(relative)
    if missing:
        raise ValueError("Assigned deliverables are missing or outside checkout: " + ", ".join(missing))


def record_ready(conn, project, task, work, setup):
    """Supersede old setup failures with current host proof before building a packet."""
    if not setup.passed:
        raise ValueError("Cannot record failed preparation as ready")
    if not workflow.enabled(project):
        return
    from . import checkpoints, repo
    current = db.get_task(conn, task["id"])
    if current is None:
        raise ValueError("Task not found")
    fields: dict[str, str | None] = {}
    if str(current.get("blocker") or "").startswith("gate `worktree_setup`"):
        fields["blocker"] = None
        if current.get("next_action") == current.get("blocker"):
            fields["next_action"] = None
    if fields:
        db.update_task(conn, task["id"], **fields)
    db.record_gate(conn, task["id"], "worktree_setup", repo.head_commit(work), True, setup.summary())
    checkpoints.write_mechanical(conn, project.root, work, task["id"], "host_preparation_ready")
