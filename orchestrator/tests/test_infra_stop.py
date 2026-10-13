"""An infrastructure problem stops the task and waits for the user; it is never a code verdict.

The benchmark lost 40% of its tokens to one loop: a reviewer whose own tools were
denied answered CHANGES, the builder was sent back to redo a correct commit, and
the same commit was reviewed and rejected again. A review that could not be done,
a worker whose tool or setup failed, and a builder that resubmits a commit review
already rejected now stop the task until the user retries; the rest of the job continues.
"""
from __future__ import annotations

import pytest

from agentkit import (
    db,
    infra_stop,
    jobs,
    mcp_server,
    repo,
    reviews,
    runner,
    supervisor,
    worktrees,
)
from agentkit import statemachine as sm
from agentkit.config import load_project
from tests.conftest import commit_all

DENIED = "audit_diff was rejected: approval is required but the runtime policy is never"


def job_task(conn, root, *, status="REVIEW"):
    jobs.create(root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=1 WHERE id='feature'")
    task_id = db.create_task(conn, title="Bound retry", spec_id="bound", job_id="feature", status=status,
                             generation=1, expected_write=["services/retry.py"], owned_paths=["services/retry.py"])
    project = load_project(root)
    work, _ = worktrees.ensure(root, db.get_task(conn, task_id), project)
    base = repo.head_commit(work)
    (work / "services/retry.py").write_text("LIMIT = 30\n")
    commit_all(work, "Bound the retry delay")
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    return project, task_id, work, repo.head_commit(work)


def job_state(conn):
    return dict(conn.execute("SELECT status,last_error FROM jobs WHERE id='feature'").fetchone())


def test_a_review_that_could_not_be_done_stops_the_task_not_the_builder(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.BLOCKED and task["attempts"] == 0
    meta = infra_stop.meta(task)
    assert meta["reason"] == infra_stop.REASON and meta["stage"] == "review" and meta["head"] == head
    assert DENIED in task["blocker"]
    assert job_state(conn) == {"status": "ACTIVE", "last_error": None}
    verdict = conn.execute("SELECT verdict FROM reviews WHERE task_id=?", (task_id,)).fetchone()[0]
    assert verdict == "BLOCKED"
    assert db.recent_events(conn, kind="infrastructure_stop", limit=1)


def test_a_stopped_task_launches_no_manager_review_or_worker(conn, project_root, monkeypatch):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    monkeypatch.setattr(supervisor, "_control_launch",
                        lambda *args, **kwargs: pytest.fail("a stopped task must not start a manager or reviewer"))
    supervisor.tick(project_root)
    from agentkit import scheduler
    assert all(t["id"] != task_id for t in scheduler.eligible_tasks(conn))
    assert db.get_task(conn, task_id)["status"] == sm.BLOCKED


def test_retry_resumes_the_review_of_the_same_commit(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    message = infra_stop.retry(conn, project, task_id)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.REVIEW and not task["blocked_meta"] and not task["blocker"]
    assert job_state(conn) == {"status": "ACTIVE", "last_error": None}
    assert "review" in message.lower()
    with pytest.raises(ValueError, match="not stopped"):
        infra_stop.retry(conn, project, task_id)


def test_retrying_one_stopped_task_leaves_the_other_stopped(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    other = db.create_task(conn, title="Other", spec_id="other", job_id="feature", status="RUNNING",
                           generation=1, owned_paths=["services/other.py"])
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    infra_stop.stop(conn, other, "worker", "Guard rejected Get-Content: sandbox denied the read")
    infra_stop.retry(conn, project, task_id)
    assert db.get_task(conn, other)["status"] == sm.BLOCKED
    infra_stop.retry(conn, project, other)
    assert job_state(conn)["status"] == "ACTIVE"
    assert db.get_task(conn, other)["status"] == sm.READY


def test_retry_never_lifts_a_job_waiting_for_a_user_decision(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    conn.execute("UPDATE jobs SET status='BLOCKED',last_error='Which retry limit does the user want?' WHERE id='feature'")
    infra_stop.retry(conn, project, task_id)
    assert job_state(conn) == {"status": "BLOCKED", "last_error": "Which retry limit does the user want?"}


def test_a_worker_reports_an_infrastructure_problem(conn, project_root, monkeypatch):
    _, task_id, _, _ = job_task(conn, project_root, status="RUNNING")
    for key, value in {"AGENTKIT_ROOT": str(project_root), "AGENTKIT_TASK": str(task_id),
                       "AGENTKIT_GENERATION": "1", "AGENTKIT_ROLE": "worker"}.items():
        monkeypatch.setenv(key, value)
    result = mcp_server.task_status("BLOCKED", "commit_and_gate denied: approval required", infrastructure=True)
    assert "stopped" in result.lower()
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.BLOCKED and infra_stop.meta(task)["stage"] == "worker"
    assert job_state(conn)["status"] == "ACTIVE"


def test_an_ordinary_worker_blocker_stays_a_planning_question(conn, project_root, monkeypatch):
    _, task_id, _, _ = job_task(conn, project_root, status="RUNNING")
    for key, value in {"AGENTKIT_ROOT": str(project_root), "AGENTKIT_TASK": str(task_id),
                       "AGENTKIT_GENERATION": "1", "AGENTKIT_ROLE": "worker"}.items():
        monkeypatch.setenv(key, value)
    mcp_server.task_status("BLOCKED", "The spec does not say which limit applies")
    assert not infra_stop.meta(db.get_task(conn, task_id))
    assert job_state(conn)["status"] == "ACTIVE"


def test_an_unchanged_commit_is_never_sent_back_to_review(conn, project_root):
    project, task_id, work, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "CHANGES", "codex:process-4", "Delay limit is not enforced")
    db.set_status(conn, task_id, sm.READY, cause="manager requeued the builder")
    db.update_task(conn, task_id, status=sm.RUNNING, generation=2)
    runner.finish_worker(conn, project_root, {"task_id": task_id, "generation": 2, "provider": "codex"}, 0, "")
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.BLOCKED
    meta = infra_stop.meta(task)
    assert meta["stage"] == "unchanged" and meta["head"] == head
    assert "Delay limit is not enforced" in task["blocker"]
    assert job_state(conn)["status"] == "ACTIVE"
    assert [r[0] for r in conn.execute("SELECT verdict FROM reviews WHERE task_id=?", (task_id,))] == ["CHANGES"]


def test_a_new_commit_after_review_goes_to_review_as_before(conn, project_root):
    project, task_id, work, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "CHANGES", "codex:process-4", "Delay limit is not enforced")
    db.set_status(conn, task_id, sm.READY, cause="manager requeued the builder")
    db.update_task(conn, task_id, status=sm.RUNNING, generation=2)
    (work / "services/retry.py").write_text("LIMIT = 30\n\ndef bounded(delay):\n    return min(delay, LIMIT)\n")
    commit_all(work, "Enforce the limit")
    runner.finish_worker(conn, project_root, {"task_id": task_id, "generation": 2, "provider": "codex"}, 0, "")
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW
    assert job_state(conn)["status"] == "ACTIVE"


def test_only_the_host_or_operator_may_resume_a_stopped_review():
    assert sm.can(sm.REVIEW, sm.BLOCKED, "reviewer")
    assert not sm.can(sm.REVIEW, sm.BLOCKED, "scheduler") and not sm.can(sm.REVIEW, sm.BLOCKED, "agent")
    assert sm.can(sm.BLOCKED, sm.REVIEW, "scheduler") and sm.can(sm.BLOCKED, sm.REVIEW, "human")
    assert not sm.can(sm.BLOCKED, sm.REVIEW, "agent")


def test_the_cli_retries_a_stopped_task(conn, project_root, capsys, monkeypatch):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    for name in ("AGENTKIT_TASK", "AGENTKIT_PROCESS"):
        monkeypatch.delenv(name, raising=False)
    from agentkit.cli import main
    assert main(["--path", str(project_root), "task", "retry", str(task_id)]) == 0
    assert "review" in capsys.readouterr().out.lower()
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW
