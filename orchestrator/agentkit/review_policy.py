"""Review choices: every task (ai), once per job (once), tests only (off), or the user (human).

`ai` sends each finished task to an independent AI reviewer. `once` and `off` let a
task's passing gate approve it for merging (GATE_REVIEWER); `once` then reviews the
combined result of the whole job one time before code accepts it (job_acceptance),
and `off` accepts on the combined checks alone. Mechanical approval is never a
human or product verdict.
"""
from __future__ import annotations

from . import (
    db,
    integrator,
    jobs,
    manager_state,
    processes,
    repo,
    reviews,
    test_packets,
    workflow,
)
from .locking import exclusive

STAGING_REVIEWER = "automation:human-staging"
#: A task approved by its passing gate alone, under review `once` or `off`.
GATE_REVIEWER = "automation:gate"
MODES = ("ai", "once", "off", "human")


def mode(project):
    section = project.raw.get("workflow") or {}
    value = section.get("review", "ai")
    if value not in MODES:
        raise ValueError("workflow.review must be ai, once, off or human")
    if value != "ai" and not workflow.enabled(project):
        raise ValueError(f"{value} review requires workflow.mode: feature or separate-tasks")
    return value


def pass_gate(conn, project, task):
    """Review `once` or `off`: the task's passing gate approves it for merging; no AI reads it now."""
    if mode(project) not in ("once", "off"):
        raise PermissionError("gate approval is available only when the project reviews once per job or not at all")
    if not task.get("job_id"):
        raise ValueError("gate approval requires a planned job")
    if any(process["task_id"] == task["id"] for process in processes.owning(conn)):
        raise ValueError("task still has a live or uncertain process owner")
    manager_state.require_clear(conn, task["job_id"])
    runtime = conn.execute("SELECT * FROM jobs WHERE id=?", (task["job_id"],)).fetchone()
    job = jobs.load(project.root, task["job_id"])
    if not runtime or runtime["status"] != "ACTIVE" or runtime["planned_revision"] != job["revision"]:
        raise ValueError("gate approval awaits the current authorized task graph")
    workflow.validate_task(project, task)
    pre = integrator.verify(conn, project, task)
    if not pre.ok:
        raise ValueError(pre.detail)
    head = repo.head_commit(task["worktree"])
    reviews.approve(conn, project, task["id"], head, "PASS", GATE_REVIEWER,
                    f"Task gate passed at {head[:12]}; review is {mode(project)}, so no AI reviewed this task alone.")


def validate_graph(project, tasks):
    if mode(project) != "human" or workflow.feature(project):
        return  # a feature builder writes and runs its own tests
    for builder in tasks:
        if builder["status"] == "CANCELLED" or builder["kind"] == "RESEARCH":
            continue
        if builder["role"] not in ("backend-builder", "frontend-builder"):
            continue
        testers = [task for task in tasks if task["status"] != "CANCELLED"
                   and test_packets.is_tester(project, task)]
        if not any(builder["id"] in {item["id"] for item in
                   test_packets.dependencies(project, tester, tasks)} for tester in testers):
            raise ValueError(f"human-review builder {builder['id']} needs an independent tester")


def stage(conn, project, task):
    """Approve only mechanical integration preconditions, with an explicit actor."""
    if mode(project) != "human":
        raise PermissionError("mechanical staging is available only in human-review mode")
    if not task.get("job_id"):
        raise ValueError("human staging requires a planned job")
    if any(process["task_id"] == task["id"] for process in processes.owning(conn)):
        raise ValueError("task still has a live or uncertain process owner")
    manager_state.require_clear(conn, task["job_id"])
    with exclusive(project.root, "jobs"):
        runtime = conn.execute("SELECT * FROM jobs WHERE id=?", (task["job_id"],)).fetchone()
        job = jobs.load(project.root, task["job_id"])
        if job.get("workflow_review", "human") != "human":
            raise ValueError("job was not configured for human staging")
        if not runtime or runtime["status"] != "ACTIVE" or runtime["planned_revision"] != job["revision"]:
            raise ValueError("human staging awaits the current authorized task graph")
        tasks = [item for item in db.list_tasks(conn) if item.get("job_id") == task["job_id"]]
        validate_graph(project, tasks)
        workflow.validate_task(project, task)
        pre = integrator.verify(conn, project, task)
        if not pre.ok:
            raise ValueError(pre.detail)
        head = repo.head_commit(task["worktree"])
        reviews.approve(conn, project, task["id"], head, "PASS", STAGING_REVIEWER,
                        "Mechanical scope/identity and task checks passed; staged preview only. "
                        "No AI review or human feature acceptance has occurred.")
