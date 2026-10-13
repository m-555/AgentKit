"""The manager plans once, then wakes only for decisions, in its own session, with a short digest.

On the benchmark every review finding woke a fresh manager session that re-read a
22-48k character job brief before requeueing the builder: 9 manager sessions, half
of all tokens. Review findings now go straight back to the builder; the manager
continues its own session and reads only what needs its judgment.
"""
from __future__ import annotations

import pytest
import yaml

from agentkit import (
    adapters,
    db,
    jobs,
    manager_digest,
    review_retry,
    reviews,
    supervisor,
    workflow,
)
from agentkit import statemachine as sm
from agentkit.config import ProjectConfig, load_project
from tests.test_infra_stop import job_task


def mode(project_root, value):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": value, "review": "ai"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


@pytest.fixture
def quiet(monkeypatch):
    launched = []
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    monkeypatch.setattr(supervisor, "_control_launch", lambda conn, project, job, purpose, task=None:
                        launched.append(purpose))
    return launched


@pytest.mark.parametrize("workflow_mode", ["feature", "separate-tasks", None])
def test_review_findings_go_back_to_the_builder_without_the_manager(conn, project_root, quiet, workflow_mode):
    if workflow_mode:
        mode(project_root, workflow_mode)
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "CHANGES", "codex:process-4", "Delay limit is not enforced")
    supervisor.tick(project_root)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.READY and task["attempts"] == 1 and not task["blocker"]
    assert "Delay limit is not enforced" in task["next_action"]
    assert "coordinator" not in quiet
    assert db.recent_events(conn, kind="review_retry", limit=1)


@pytest.mark.parametrize("verdict", ["CHANGES", "REJECT"])
def test_after_the_last_retry_or_a_rejection_the_user_decides(conn, project_root, quiet, verdict):
    mode(project_root, "feature")
    project, task_id, _, head = job_task(conn, project_root)
    if verdict == "CHANGES":
        db.update_task(conn, task_id, attempts=review_retry.MAX_ATTEMPTS)
    reviews.approve(conn, project, task_id, head, verdict, "codex:process-4", "Wrong layer; the limit belongs in config")
    supervisor.tick(project_root)
    assert db.get_task(conn, task_id)["status"] == sm.FAILED
    assert "coordinator" not in quiet
    from agentkit import problems
    assert [(p["task_id"], p["kind"]) for p in problems.listing(conn)] == [(task_id, "review")]


@pytest.mark.parametrize("workflow_mode", ["feature", "separate-tasks"])
def test_the_manager_resumes_its_own_session(tmp_path, workflow_mode):
    project = ProjectConfig(root=tmp_path, raw={"workflow": {"mode": workflow_mode}})
    assert workflow.resume_token(project, {"session_token": "manager"}, True, role="coordinator") == "manager"
    assert workflow.resume_token(project, {"session_token": "manager"}, False, role="coordinator") is None


def test_codex_resumes_the_manager_session_in_separate_tasks(tmp_path, monkeypatch):
    project = ProjectConfig(root=tmp_path, raw={"workflow": {"mode": "separate-tasks"}})
    adapter = adapters.get("codex")
    monkeypatch.setattr(adapter, "detect", lambda: None)
    manager = adapter.build_launch({"id": 1}, tmp_path, "coordinator", project, prompt="Wake", resume_token="manager")
    assert "resume" in manager.argv and "manager" in manager.argv
    builder = adapter.build_launch({"id": 1}, tmp_path, "backend-builder", project, prompt="Go", resume_token="old")
    assert "old" not in builder.argv


def test_a_resumed_manager_reads_a_short_digest_of_decisions(conn, project_root):
    project = mode(project_root, "feature")
    _, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "REJECT", "codex:process-4", "Wrong layer; " + "detail " * 300)
    done = db.create_task(conn, title="Docs", spec_id="docs", job_id="feature", status="DONE")
    text = manager_digest.digest(conn, project, jobs.load(project_root, "feature"))
    assert len(text) <= manager_digest.LIMIT
    assert f"Task {task_id} FAILED" in text and "Wrong layer" in text
    assert "1 DONE" in text and f"Task {done} " not in text
    prompt = manager_digest.wake_prompt(conn, project, jobs.load(project_root, "feature"))
    assert "same session" in prompt and "job_brief" in prompt and text in prompt


def test_a_finished_job_is_accepted_by_code_with_a_workflow(conn, project_root):
    project = mode(project_root, "feature")
    jobs.create(project_root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    db.create_task(conn, title="Bound retry", spec_id="bound", job_id="feature", status="DONE")
    text = manager_digest.digest(conn, project, jobs.load(project_root, "feature"))
    assert "code accepts the finished job" in text and "job_accept" not in text
    legacy = manager_digest.digest(conn, load_project(project_root).__class__(root=project_root),
                                   jobs.load(project_root, "feature"))
    assert "job_accept" in legacy


def test_a_fresh_manager_still_plans_from_the_full_brief(conn, project_root):
    project = mode(project_root, "feature")
    jobs.create(project_root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    fresh = supervisor.coordinator_body(conn, project, jobs.load(project_root, "feature"), resumed=False)
    assert "Call job_brief" in fresh and "one task per feature" in fresh
    resumed = supervisor.coordinator_body(conn, project, jobs.load(project_root, "feature"), resumed=True)
    assert "same session" in resumed and "Call job_brief for" not in resumed
