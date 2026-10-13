"""Review every task, once per job, or not at all; code accepts the finished job.

The manager used to wake when every task was done to check the combined result,
repeating what the reviews had seen. `review: once` lets each task's passing gate
approve it and reviews the whole job one time after its tasks merge; `off` relies
on the tests; `ai` reviews every task as before. In every mode code accepts the
job, and the assigner is never woken for it.
"""
from __future__ import annotations

import pytest
import yaml

from agentkit import (
    codex_mcp_policy,
    db,
    integrator,
    job_acceptance,
    jobs,
    mcp_extra,
    problems,
    repo,
    review_policy,
    reviews,
    supervisor,
)
from agentkit import statemachine as sm
from agentkit.config import ProjectConfig, load_project
from tests.test_infra_stop import job_state, job_task


def configure(project_root, mode="feature", review="once"):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": mode, "review": review}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


@pytest.fixture
def quiet(monkeypatch):
    launched = []
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    monkeypatch.setattr(supervisor, "_control_launch", lambda conn, project, job, purpose, task=None:
                        launched.append(purpose))
    return launched


def combined(project):
    return repo.head_commit(integrator.integration_worktree(project))


def merged_job(conn, project_root, review):
    """One finished feature task: gate-approved (or reviewed) and merged."""
    configure(project_root, review=review)
    project, task_id, _, head = job_task(conn, project_root)
    if review == "ai":
        reviews.approve(conn, project, task_id, head, "PASS", "codex:process-4", "Read the diff; limit enforced")
    supervisor.tick(project_root)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.DONE, task
    return project, task_id


def test_the_review_choices(tmp_path):
    for value in ("ai", "once", "off", "human"):
        assert review_policy.mode(ProjectConfig(root=tmp_path, raw={"workflow": {"mode": "feature", "review": value}})) == value
    with pytest.raises(ValueError, match="ai, once, off or human"):
        review_policy.mode(ProjectConfig(root=tmp_path, raw={"workflow": {"mode": "feature", "review": "sometimes"}}))
    with pytest.raises(ValueError, match=r"once review requires workflow\.mode"):
        review_policy.mode(ProjectConfig(root=tmp_path, raw={"workflow": {"review": "once"}}))


@pytest.mark.parametrize("review", ["once", "off"])
def test_a_task_whose_gate_passes_merges_without_a_reviewer(conn, project_root, quiet, review):
    project, task_id = merged_job(conn, project_root, review)
    approval = conn.execute("SELECT reviewer,verdict FROM reviews WHERE task_id=?", (task_id,)).fetchone()
    assert (approval["reviewer"], approval["verdict"]) == (review_policy.GATE_REVIEWER, "PASS")
    assert "review" not in quiet and "coordinator" not in quiet


def test_a_gate_approval_does_not_count_as_a_per_task_review(conn, project_root, quiet):
    _, task_id = merged_job(conn, project_root, "once")
    configure(project_root, review="ai")
    with pytest.raises(ValueError, match="gate-only approval"):
        reviews.require_approval(conn, db.get_task(conn, task_id), load_project(project_root))


def test_off_accepts_the_finished_job_by_code(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "off")
    supervisor.tick(project_root)
    state = dict(conn.execute("SELECT status,completed_sha FROM jobs WHERE id='feature'").fetchone())
    assert state["status"] == "DONE" and state["completed_sha"] == combined(project)
    assert quiet == []


def test_per_task_review_is_accepted_by_code_without_the_assigner(conn, project_root, quiet):
    merged_job(conn, project_root, "ai")
    supervisor.tick(project_root)
    assert job_state(conn)["status"] == "DONE"
    assert "coordinator" not in quiet


def test_once_reviews_the_whole_job_then_accepts(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "once")
    supervisor.tick(project_root)
    assert quiet == ["final_review"] and job_state(conn)["status"] == "ACTIVE"
    job_acceptance.record(conn, project, "feature", combined(project), "PASS", "codex:process-9",
                          "Read the combined change; every request and acceptance item is met")
    supervisor.tick(project_root)
    assert job_state(conn)["status"] == "DONE"
    assert quiet == ["final_review"]


def test_final_review_findings_wait_for_the_user(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "once")
    supervisor.tick(project_root)
    job_acceptance.record(conn, project, "feature", combined(project), "CHANGES", "codex:process-9",
                          "services/retry.py:1 the limit is not configurable as requested")
    supervisor.tick(project_root)
    state = job_state(conn)
    assert state["status"] == "BLOCKED" and state["last_error"].startswith(job_acceptance.FINAL)
    (row,) = [p for p in problems.listing(conn) if p["kind"] == "final"]
    assert row["options"] == ["accept", "retry", "plan"] and "not configurable" in row["detail"]
    assert "coordinator" not in quiet
    problems.act_locked(conn, project, row["code"], "accept", by="phone")
    supervisor.tick(project_root)
    assert job_state(conn)["status"] == "DONE"


def test_retry_asks_for_a_new_final_review(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "once")
    supervisor.tick(project_root)
    job_acceptance.record(conn, project, "feature", combined(project), "BLOCKED", "codex:process-9",
                          "source_read was denied")
    supervisor.tick(project_root)
    (row,) = [p for p in problems.listing(conn) if p["kind"] == "final"]
    assert "could not review" in row["detail"]
    problems.act_locked(conn, project, row["code"], "retry")
    supervisor.tick(project_root)
    assert quiet == ["final_review", "final_review"] and job_state(conn)["status"] == "ACTIVE"


def test_plan_sends_the_findings_to_the_assigner_without_extra_words(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "once")
    supervisor.tick(project_root)
    job_acceptance.record(conn, project, "feature", combined(project), "CHANGES", "codex:process-9",
                          "The retry limit must come from config")
    supervisor.tick(project_root)
    (row,) = [p for p in problems.listing(conn) if p["kind"] == "final"]
    problems.act_locked(conn, project, row["code"], "plan")
    request = jobs.load(project_root, "feature")["requests"][-1]["text"]
    assert request.startswith("Fix what the final review found:") and "come from config" in request
    supervisor.tick(project_root)
    assert "coordinator" in quiet


def test_a_final_reviewer_that_stops_twice_is_a_problem(conn, project_root, quiet):
    project, _ = merged_job(conn, project_root, "once")
    head = combined(project)
    for _ in range(2):
        conn.execute("INSERT INTO processes(purpose,provider,job_id,generation,expected_head,launch_json,started_at,"
                     "status,ended_at) VALUES('final_review','codex','feature',0,?,'{}',?,'FAILED',?)",
                     (head, db.utcnow(), db.utcnow()))
    supervisor.tick(project_root)
    assert quiet == [] and job_state(conn)["status"] == "BLOCKED"
    assert [p["kind"] for p in problems.listing(conn)] == ["final"]


def test_failed_combined_checks_wait_for_the_user(conn, project_root, quiet, monkeypatch):
    project, _ = merged_job(conn, project_root, "off")
    monkeypatch.setattr(supervisor, "accept_job", lambda *args: (_ for _ in ()).throw(
        ValueError("combined result failed verification: full gate failed")))
    supervisor.tick(project_root)
    (row,) = problems.listing(conn)
    assert row["kind"] == "accept" and row["options"] == ["retry", "plan"] and "full gate failed" in row["detail"]
    monkeypatch.undo()
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    problems.act_locked(conn, project, row["code"], "retry")
    supervisor.tick(project_root)
    assert job_state(conn)["status"] == "DONE"


def test_a_job_whose_tasks_were_all_skipped_asks_the_user(conn, project_root, quiet):
    configure(project_root, review="once")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, status="CANCELLED")
    supervisor.tick(project_root)
    (row,) = problems.listing(conn)
    assert row["kind"] == "accept" and "skipped" in row["detail"]
    assert quiet == []


def test_only_the_final_reviewer_records_a_job_review(conn, project_root, monkeypatch):
    project, _ = merged_job(conn, project_root, "once")
    head = combined(project)
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_PROCESS", raising=False)
    with pytest.raises(PermissionError):
        mcp_extra.job_review_submit("feature", head, "PASS", "Looks right")
    conn.execute("INSERT INTO processes(purpose,provider,job_id,generation,expected_head,launch_json,"
                 "started_at,status) VALUES('final_review','codex','feature',0,?,'{}',?,'RUNNING')",
                 (head, db.utcnow()))
    process = conn.execute("SELECT id FROM processes WHERE purpose='final_review'").fetchone()[0]
    monkeypatch.setenv("AGENTKIT_PROCESS", str(process))
    with pytest.raises(ValueError, match="changed"):
        mcp_extra.job_review_submit("feature", "0" * 40, "PASS", "Looks right")
    assert mcp_extra.job_review_submit("feature", head, "pass", "Read every change") == "Final review recorded."
    assert job_acceptance.latest(conn, "feature")["verdict"] == "PASS"


def test_codex_reviewers_may_submit_a_job_review_without_a_prompt():
    flags = codex_mcp_policy.approval_flags("reviewer", readonly=True)
    assert 'mcp_servers.agentkit.tools.job_review_submit.approval_mode="approve"' in flags


def test_the_final_reviewer_gets_the_intent_and_the_combined_change(conn, project_root):
    project, task_id = merged_job(conn, project_root, "once")
    job = jobs.load(project_root, "feature")
    tasks = [t for t in db.list_tasks(conn) if t.get("job_id") == "feature"]
    text = job_acceptance.body(conn, project, job, tasks, combined(project))
    assert "job_review_submit(job_id='feature'" in text and "Delay never exceeds the limit" in text
    assert "services/retry.py" in text and "+LIMIT = 30" in text and f"task {task_id}" in text


def test_switching_between_ai_once_and_off_keeps_the_job_running(conn, project_root, quiet):
    configure(project_root, review="ai")
    job_task(conn, project_root, status="RUNNING")
    configure(project_root, review="once")
    supervisor.tick(project_root)
    assert job_state(conn)["status"] == "ACTIVE"


def test_the_assigner_is_told_it_only_plans(conn, project_root):
    project = configure(project_root, review="once")
    jobs.create(project_root, "feature", "Bound the retry delay")
    body = supervisor.coordinator_body(conn, project, jobs.load(project_root, "feature"), resumed=False)
    assert "You are the assigner" in body and "job_accept" not in body


def test_new_projects_review_once(repo):
    from agentkit.init_project import init
    init(repo)
    assert review_policy.mode(load_project(repo)) == "once"


def test_the_view_saves_each_review_choice(project_root):
    from agentkit import repo_settings
    configure(project_root, review="ai")
    for value in ("once", "off", "human", "ai"):
        repo_settings.apply(project_root, {"review": value})
        assert repo_settings.read(project_root)["review"] == value
    with pytest.raises(ValueError, match="ai, once, off or human"):
        repo_settings.apply(project_root, {"review": "later"})
