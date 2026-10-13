"""The view tells the operator a task stopped on an infrastructure problem and lets them retry."""
from __future__ import annotations

from agentkit import (
    db,
    infra_stop,
    repo_controls,
    repo_settings,
    reviews,
)
from agentkit import statemachine as sm
from tests.test_infra_stop import DENIED, job_task
from tests.test_view_home import call, home_view


def stopped_review(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    return task_id


def test_settings_list_every_task_stopped_on_infrastructure(conn, project_root):
    task_id = stopped_review(conn, project_root)
    shown = repo_settings.read(project_root)["stopped"]
    assert [(row["task"], row["stage"], row["job"]) for row in shown] == [(task_id, "review", "feature")]
    assert DENIED in shown[0]["reason"]


def test_retry_from_the_view_resumes_the_review(conn, project_root, monkeypatch):
    task_id = stopped_review(conn, project_root)
    monkeypatch.setattr(repo_controls, "_call", lambda path, body=None, timeout=5: {"thinkingOverride": None})
    with home_view(project_root) as server:
        token = call(server, "/api/reviews")[1]["token"]
        post = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        status, result = call(server, "/api/task-retry", body={"task": task_id}, headers=post)
        assert status == 200 and "review" in result["message"].lower()
        assert call(server, "/api/task-retry", body={"task": task_id}, headers=post)[0] == 409
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW
    assert not infra_stop.stopped(conn)
    assert repo_settings.read(project_root)["stopped"] == []
