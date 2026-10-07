"""One write path for the committed task graph, shared by CLI and coordinator."""
from __future__ import annotations

from pathlib import Path

from . import db, reconcile, spec
from .locking import exclusive


def put(root: str | Path, task: spec.TaskSpec) -> int:
    task.validate()
    from .config import load_project
    from .workflow import validate_task
    validate_task(load_project(root), task)
    from .environment_profiles import select
    project = load_project(root)
    # Review/verification select by gate, without a live worker role fallback.
    select(project, level=task.gate_level)
    if task.model_assignment is not None:
        from .config import load_project
        from .policy import for_task
        # Resolve against the project now, so an unknown or unsafe pin is rejected
        # at definition rather than discovered by a stalled scheduler.
        for_task(load_project(root), {**task.to_dict(), "spec_id": task.spec_id})
    with exclusive(root, "spec"):
        tasks = spec.load(root)
        existing = next((t for t in tasks if t.spec_id == task.spec_id), None)
        conn = db.connect(root)
        try:
            live = db.get_task_by_spec(conn, task.spec_id)
            if live and conn.execute("SELECT id FROM processes WHERE task_id=? AND status IN ('STARTING','RUNNING')", (live["id"],)).fetchone():
                raise ValueError("old worker is still running")
            if live and live["status"] not in ("PLANNED", "READY", "NEEDS_REPLAN"):
                raise ValueError("stop the worker and resolve its state before changing its task")
        finally:
            conn.close()
        if existing:
            tasks[tasks.index(existing)] = task
        else:
            tasks.append(task)
        from .test_packets import dependencies
        dependencies(load_project(root), task, tasks)
        spec.save(root, tasks)
        reconcile.reconcile(root, adopt_running=True)
        conn = db.connect(root)
        try:
            record = db.get_task_by_spec(conn, task.spec_id)
            assert record is not None
            if record["status"] == "NEEDS_REPLAN":
                # The old run must have ended before the coordinator can requeue it.
                active = conn.execute("SELECT id FROM processes WHERE task_id=? AND status IN ('STARTING','RUNNING')", (record["id"],)).fetchone()
                if active:
                    raise ValueError("old worker is still running")
                db.set_status(conn, record["id"], "PLANNED", cause="coordinator revised task")
                # Reconcile must copy the new definition after a replan.
                fields: dict[str, str | None] = {"spec_hash": ""}
                # This one hold is produced by reconcile itself. A stopped,
                # explicitly revised definition resolves it; concrete execution
                # holds and quota metadata still need their own recovery path.
                if record.get("blocker") == "spec changed while task was in flight":
                    fields["blocker"] = None
                    fields["next_action"] = None
                db.update_task(conn, record["id"], **fields)
        finally:
            conn.close()
        reconcile.reconcile(root, adopt_running=True)
        return int(record["id"])


def create(root: str | Path, *, title: str, depends_on: list[int] | None = None, **fields) -> int:
    conn = db.connect(root)
    try:
        dependencies = []
        for identifier in depends_on or []:
            task = db.get_task(conn, identifier)
            if not task or not task.get("spec_id"):
                raise ValueError(f"dependency {identifier} has no committed specification")
            dependencies.append(task["spec_id"])
    finally:
        conn.close()
    slug = fields.pop("spec_id", None) or spec.slugify(title)
    return put(root, spec.TaskSpec(spec_id=slug, title=title, depends_on=dependencies, **fields))
