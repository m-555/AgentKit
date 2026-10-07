"""Two review choices. Mechanical staging is never a human/product verdict."""
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


def mode(project):
    section = project.raw.get("workflow") or {}
    value = section.get("review", "ai")
    if value not in ("human", "ai"):
        raise ValueError("workflow.review must be human or ai")
    if value == "human" and not workflow.enabled(project):
        raise ValueError("human review requires workflow.mode: separate-tasks")
    return value


def validate_graph(project, tasks):
    if mode(project) != "human":
        return
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
