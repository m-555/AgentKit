"""A stopped task waits for the user while the rest of its job keeps going.

After the assigner has planned a job, nothing wakes a model to decide what a stop
means: code records the problem, what happened and which replies can fix it.
Tasks that do not depend on the stopped one keep running and merging, and the
cheapest reply that fixes the problem spends no tokens at all.
"""
from __future__ import annotations

import pytest
import yaml

from agentkit import (
    db,
    infra_stop,
    jobs,
    problems,
    review_retry,
    reviews,
    scheduler,
    supervisor,
)
from agentkit import statemachine as sm
from agentkit.config import load_project
from tests.conftest import commit_all
from tests.test_infra_stop import DENIED, job_state, job_task


def mode(project_root, value, review="ai"):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": value, "review": review}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


@pytest.fixture
def quiet(monkeypatch):
    launched = []
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    monkeypatch.setattr(supervisor, "_control_launch", lambda conn, project, job, purpose, task=None:
                        launched.append(purpose))
    return launched


def other_task(conn, *, status="READY", **extra):
    return db.create_task(conn, title="Other", spec_id="other", job_id="feature", status=status, generation=1,
                          expected_write=["services/other.py"], owned_paths=["services/other.py"], **extra)


def open_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM problems WHERE status='OPEN' ORDER BY id")]


def test_an_infrastructure_stop_holds_the_task_and_the_job_keeps_going(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    other = other_task(conn)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    assert db.get_task(conn, task_id)["status"] == sm.BLOCKED
    assert job_state(conn)["status"] == "ACTIVE"
    assert [t["id"] for t in scheduler.eligible_tasks(conn)] == [other]


def test_a_stop_becomes_one_problem_with_a_code_and_its_replies(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    opened = problems.sync(conn, project)
    assert len(opened) == 1
    row = opened[0]
    assert row["task_id"] == task_id and row["job_id"] == "feature" and row["kind"] == "setup"
    assert len(row["code"]) == 6 and row["code"].isalnum()
    # A review that could not run has no builder to hint: retry, skip or re-plan.
    assert row["options"] == ["retry", "skip", "plan"]
    assert DENIED in row["detail"]
    assert problems.sync(conn, project) == []  # the same stop is never reported twice
    assert len(open_rows(conn)) == 1


def test_retry_clears_the_problem(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    code = problems.sync(conn, project)[0]["code"]
    message = problems.act_locked(conn, project, code.lower(), "retry", by="phone")
    assert "review" in message.lower()
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW
    problems.sync(conn, project)
    assert open_rows(conn) == []
    row = conn.execute("SELECT status,resolution FROM problems WHERE code=?", (code,)).fetchone()
    assert row["status"] == "RESOLVED" and "retry" in row["resolution"] and "phone" in row["resolution"]


def test_a_reply_that_does_not_fit_the_problem_is_refused(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    code = problems.sync(conn, project)[0]["code"]
    with pytest.raises(ValueError, match="retry, skip or plan"):
        problems.act_locked(conn, project, code, "accept")
    with pytest.raises(ValueError, match="not open"):
        problems.act_locked(conn, project, "ZZZZZZ", "retry")
    assert db.get_task(conn, task_id)["status"] == sm.BLOCKED


def test_the_same_setup_problem_in_two_tasks_holds_the_job_until_one_is_retried(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root, status="RUNNING")
    other = other_task(conn, status="RUNNING")
    infra_stop.stop(conn, task_id, "worker", "Guard rejected Get-Content: sandbox denied the read")
    problems.sync(conn, project)
    assert job_state(conn)["status"] == "ACTIVE"
    infra_stop.stop(conn, other, "worker", "Guard rejected Get-Content: sandbox denied the read")
    problems.sync(conn, project)
    state = job_state(conn)
    assert state["status"] == "BLOCKED" and state["last_error"].startswith(problems.HOLD)  # the scheduler launches only ACTIVE jobs
    code = next(r["code"] for r in open_rows(conn) if r["task_id"] == task_id)
    problems.act_locked(conn, project, code, "retry")
    problems.sync(conn, project)
    assert job_state(conn)["status"] == "ACTIVE"


def test_the_assigner_is_not_woken_for_stops_failures_or_scope_requests(conn, project_root, quiet):
    mode(project_root, "feature")
    project, task_id, _, head = job_task(conn, project_root)
    other = other_task(conn, status="NEEDS_REPLAN")
    db.create_amendment(conn, task_id, "widen task scope to: services/extra.py", "needs a helper")
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    supervisor.tick(project_root)
    assert "coordinator" not in quiet
    kinds = sorted(r["kind"] for r in open_rows(conn))
    assert kinds == ["replan", "scope", "setup"]
    assert db.get_task(conn, other)["status"] == sm.NEEDS_REPLAN


def test_the_assigner_still_plans_a_changed_job(conn, project_root, quiet):
    mode(project_root, "feature")
    job_task(conn, project_root)
    jobs.amend(project_root, "feature", "Also log each capped delay", user=True)
    supervisor.tick(project_root)
    assert "coordinator" in quiet


def test_a_failed_gate_retries_itself_then_waits_for_the_user(conn, project_root, quiet):
    mode(project_root, "feature")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=1, blocker="gate fast failed: 2 tests failed")
    supervisor.tick(project_root)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.READY and task["attempts"] == 2
    db.update_task(conn, task_id, status="FAILED", attempts=3, blocker="gate fast failed: 1 test failed")
    supervisor.tick(project_root)
    assert db.get_task(conn, task_id)["status"] == sm.FAILED
    (row,) = open_rows(conn)
    assert row["kind"] == "failed" and row["options"] == '["retry", "hint", "skip", "plan"]'
    assert "coordinator" not in quiet


def test_a_separate_tester_failure_waits_for_the_user_at_once(conn, project_root, quiet):
    mode(project_root, "separate-tasks")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, role="backend-tester", kind="TEST_ONLY", attempts=1, blocker="gate failed")
    supervisor.tick(project_root)
    assert db.get_task(conn, task_id)["status"] == sm.FAILED
    assert [r["kind"] for r in open_rows(conn)] == ["failed"]


def test_review_findings_after_the_last_retry_offer_accept(conn, project_root, quiet):
    mode(project_root, "feature")
    project, task_id, _, head = job_task(conn, project_root)
    db.update_task(conn, task_id, attempts=review_retry.MAX_ATTEMPTS)
    reviews.approve(conn, project, task_id, head, "CHANGES", "codex:process-4", "Name the constant MAX_DELAY")
    supervisor.tick(project_root)
    (row,) = open_rows(conn)
    assert row["kind"] == "review" and "Name the constant MAX_DELAY" in row["detail"]
    assert row["options"] == '["accept", "hint", "skip", "plan"]'
    message = problems.act_locked(conn, project, row["code"], "accept", by="view")
    assert "merge" in message.lower()
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.INTEGRATION_READY
    approval = conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    assert approval["verdict"] == "PASS" and approval["reviewer"] == "user:view" and approval["head_sha"] == head
    assert reviews.require_approval(conn, task, project) == head  # the gate passed on the accepted commit
    assert "coordinator" not in quiet


def test_accept_refuses_a_commit_whose_gate_fails(conn, project_root):
    project = mode(project_root, "feature")
    raw = yaml.safe_load((project_root / ".ai/project.yaml").read_text(encoding="utf-8"))
    raw["gates"]["fast"] = ['python -c "raise SystemExit(1)"']
    (project_root / ".ai/project.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    project, task_id, _, head = job_task(conn, project_root)
    db.update_task(conn, task_id, attempts=review_retry.MAX_ATTEMPTS, gate_level="fast")
    reviews.approve(conn, project, task_id, head, "CHANGES", "codex:process-4", "Rename it")
    (row,) = problems.sync(conn, project)
    with pytest.raises(ValueError, match="gate"):
        problems.act_locked(conn, project, row["code"], "accept")
    assert db.get_task(conn, task_id)["status"] == sm.FAILED
    assert open_rows(conn)[0]["code"] == row["code"]


def test_a_hint_returns_the_task_to_its_builder_with_the_users_words(conn, project_root):
    project = mode(project_root, "feature")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=3, blocker="gate fast failed: test_limit")
    (row,) = problems.sync(conn, project)
    with pytest.raises(ValueError, match="words"):
        problems.act_locked(conn, project, row["code"], "hint", "   ")
    problems.act_locked(conn, project, row["code"], "hint", "Use the LIMIT from config.py, not a literal")
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.READY and task["generation"] == 2 and task["attempts"] == 3
    assert task["next_action"].startswith("The user says: Use the LIMIT from config.py")
    assert not task["blocker"]


def test_skip_cancels_the_task_and_everything_that_depends_on_it(conn, project_root):
    project = mode(project_root, "feature")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=3, blocker="gate failed")
    by_id = db.create_task(conn, title="Uses it", spec_id="uses", job_id="feature", status="PLANNED",
                           generation=1, depends_on=[str(task_id)])
    by_spec = db.create_task(conn, title="Uses that", spec_id="uses-that", job_id="feature", status="PLANNED",
                             generation=1, depends_on=["uses"])
    free = other_task(conn)
    (row,) = problems.sync(conn, project)
    message = problems.act_locked(conn, project, row["code"], "skip")
    assert {db.get_task(conn, t)["status"] for t in (task_id, by_id, by_spec)} == {sm.CANCELLED}
    assert db.get_task(conn, free)["status"] == sm.READY
    assert str(by_id) in message and str(by_spec) in message


def test_plan_hands_the_users_words_to_the_assigner_and_reopens_if_still_stopped(conn, project_root):
    project = mode(project_root, "feature")
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=3, blocker="gate failed")
    (row,) = problems.sync(conn, project)
    problems.act_locked(conn, project, row["code"], "plan", "Split the config change into its own task")
    job = jobs.load(project_root, "feature")
    assert job["revision"] == 2 and "Split the config change into its own task" in job["requests"][-1]["text"]
    assert job_state(conn)["status"] == "PLANNING"
    assert problems.sync(conn, project) == [] and open_rows(conn) == []
    # The assigner re-planned but left the task stopped: it is a problem again.
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=2 WHERE id='feature'")
    (again,) = problems.sync(conn, project)
    assert again["code"] == row["code"] and "re-planned" in again["detail"]


def test_an_assigner_question_is_answered_with_plan(conn, project_root):
    project = mode(project_root, "feature")
    job_task(conn, project_root)
    conn.execute("UPDATE jobs SET status='BLOCKED',last_error='Should the limit be 30 or 60 seconds?' WHERE id='feature'")
    (row,) = problems.sync(conn, project)
    assert row["kind"] == "question" and row["task_id"] is None and row["options"] == ["plan"]
    problems.act_locked(conn, project, row["code"], "answer", "60 seconds")
    assert jobs.load(project_root, "feature")["requests"][-1]["text"].startswith("60 seconds")
    assert job_state(conn)["status"] == "PLANNING"


def test_dismiss_closes_a_scope_request(conn, project_root):
    project = mode(project_root, "feature")
    _, task_id, _, _ = job_task(conn, project_root, status="RUNNING")
    amendment = db.create_amendment(conn, task_id, "widen task scope to: services/extra.py", "needs a helper")
    (row,) = problems.sync(conn, project)
    assert row["kind"] == "scope" and "services/extra.py" in row["detail"]
    problems.act_locked(conn, project, row["code"], "dismiss")
    assert not [a for a in db.list_amendments(conn) if a["id"] == amendment]
    assert problems.sync(conn, project) == [] and open_rows(conn) == []


def test_a_task_with_a_live_session_is_never_a_problem(conn, project_root, monkeypatch):
    project = mode(project_root, "feature")
    _, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=3)
    from agentkit import processes
    monkeypatch.setattr(processes, "owning", lambda conn: [{"task_id": task_id}])
    assert problems.sync(conn, project) == []


def test_without_a_workflow_the_manager_keeps_its_old_duties_but_not_setup_stops(conn, project_root, quiet):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    supervisor.tick(project_root)
    assert "coordinator" not in quiet and [r["kind"] for r in open_rows(conn)] == ["setup"]
    other = other_task(conn, status="FAILED")
    db.update_task(conn, other, attempts=3)
    supervisor.tick(project_root)
    assert "coordinator" in quiet
    assert all(r["task_id"] != other for r in open_rows(conn))


def test_listing_shows_open_problems_with_their_task(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    problems.sync(conn, project)
    (item,) = problems.listing(conn)
    assert item["task_id"] == task_id and item["task_title"] == "Bound retry" and item["options"] == ["retry", "skip", "plan"]


def test_the_cli_lists_and_answers_problems(conn, project_root, capsys, monkeypatch):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    code = problems.sync(conn, project)[0]["code"]
    for name in ("AGENTKIT_TASK", "AGENTKIT_PROCESS"):
        monkeypatch.delenv(name, raising=False)
    from agentkit.cli import main
    assert main(["--path", str(project_root), "problem", "list"]) == 0
    assert code in capsys.readouterr().out
    assert main(["--path", str(project_root), "problem", "act", code, "retry"]) == 0
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW


def test_a_new_commit_after_a_hint_is_a_new_problem_if_it_fails_again(conn, project_root):
    project = mode(project_root, "feature")
    project, task_id, work, _ = job_task(conn, project_root, status="FAILED")
    db.update_task(conn, task_id, attempts=3, blocker="gate failed")
    (first,) = problems.sync(conn, project)
    problems.act_locked(conn, project, first["code"], "hint", "Try again with the config value")
    (work / "services/retry.py").write_text("LIMIT = 31\n")
    commit_all(work, "Try again")
    db.update_task(conn, task_id, status="FAILED", blocker="gate failed again")
    (second,) = problems.sync(conn, project)
    assert second["code"] != first["code"]
