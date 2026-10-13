"""Follow-ups from the 2026-10-12 efficiency audit that were not already covered.

A: a user update cannot push the job's intent past what managed workers accept;
long diagnostics belong in a file the update names (the pilot's evaluator feedback
put ~79k characters of repeated tracebacks into the authoritative requests).
B: a reviewer gets the exact diff, scope audit and gate receipt in its prompt.
C: a manager whose last session grew large starts fresh with the digest.
D: an optional job token budget holds the job, keeping its work, until raised.
"""
from __future__ import annotations

import json

import pytest
import yaml

from agentkit import db, jobs, review_packet, supervisor, workflow
from agentkit import statemachine as sm
from agentkit.config import load_project
from tests.test_infra_stop import job_task


def configure(root, **settings):
    path = root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.update(settings)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(root)


def test_a_long_update_is_refused_before_it_reaches_any_prompt(project_root):
    configure(project_root, workflow={"mode": "feature", "review": "ai"})
    jobs.create(project_root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    before = jobs.load(project_root, "feature")
    with pytest.raises(ValueError, match="max_context_chars") as refused:
        jobs.amend(project_root, "feature", "Traceback (most recent call last):\n" * 1000, user=True)
    assert "file" in str(refused.value)
    assert jobs.load(project_root, "feature") == before
    jobs.amend(project_root, "feature", "Also cap the delay at 30 seconds.", user=True)
    assert jobs.load(project_root, "feature")["revision"] == before["revision"] + 1


def test_unmanaged_projects_keep_accepting_long_updates(project_root):
    jobs.create(project_root, "legacy", "Bound the retry delay")
    jobs.amend(project_root, "legacy", "context " * 4000, user=True)
    assert jobs.load(project_root, "legacy")["revision"] == 2


def test_the_review_packet_carries_the_exact_change(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    db.record_gate(conn, task_id, "fast", head, True, "fast: python -c print(1) passed")
    packet = review_packet.build(conn, project, db.get_task(conn, task_id), head)
    assert "LIMIT = 30" in packet and "services/retry.py" in packet
    assert "Delay never exceeds the limit" in packet or "Bound retry" in packet
    assert "scope: clean" in packet and "passed" in packet and head[:12] in packet
    assert len(packet) <= review_packet.LIMIT + 2000


def test_a_large_diff_is_paged_not_dropped(conn, project_root):
    project, task_id, work, _ = job_task(conn, project_root)
    from tests.conftest import commit_all
    (work / "services/retry.py").write_text("".join(f"VALUE_{i} = {i}\n" for i in range(4000)))
    commit_all(work, "Large change")
    from agentkit import repo
    head = repo.head_commit(work)
    packet = review_packet.build(conn, project, db.get_task(conn, task_id), head)
    assert "task_diff" in packet and "offset" in packet
    assert len(packet) <= review_packet.LIMIT + 2000


def test_the_reviewer_prompt_includes_the_packet(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    body = supervisor.review_body(conn, project, db.get_task(conn, task_id), head)
    assert "review_submit" in body and "LIMIT = 30" in body


def usage(project_root, conn, job, tokens, provider="codex"):
    identifier = conn.execute(
        "INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at,ended_at) "
        "VALUES('coordinator',?,?,'FINISHED',1,'{}',?,?)", (provider, job, db.utcnow(), db.utcnow())).lastrowid
    folder = project_root / ".ai/runtime" / f"process-{identifier}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "usage.json").write_text(json.dumps({
        "version": 1, "parser_version": 2, "process_id": identifier, "fingerprint": [0, 0, 0, 0],
        "usage": {"input_tokens": tokens, "output_tokens": 1000, "thinking_tokens": None,
                  "cached_input_tokens": 0, "cache_write_input_tokens": 0, "complete": True}}), encoding="utf-8")
    return identifier


def test_a_manager_whose_session_grew_large_starts_fresh(conn, project_root):
    configure(project_root, workflow={"mode": "feature", "review": "ai"})
    jobs.create(project_root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    conn.execute("UPDATE jobs SET coordinator_session='manager' WHERE id='feature'")
    usage(project_root, conn, "feature", 50_000)
    assert supervisor.manager_session(conn, project_root, "feature") == "manager"
    usage(project_root, conn, "feature", supervisor.RESUME_INPUT_LIMIT + 1)
    assert supervisor.manager_session(conn, project_root, "feature") is None
    assert db.recent_events(conn, kind="manager_fresh_start", limit=1)
    body = supervisor.coordinator_body(conn, load_project(project_root), jobs.load(project_root, "feature"),
                                       resumed=False)
    assert "Current state" in body and "Job feature" in body


def test_a_job_over_its_token_budget_holds_and_resumes_when_raised(conn, project_root, monkeypatch):
    configure(project_root, workflow={"mode": "feature", "review": "ai"}, budgets={"job_tokens": 100_000})
    _, task_id, _, _ = job_task(conn, project_root)
    usage(project_root, conn, "feature", 150_000)
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    monkeypatch.setattr(supervisor, "_control_launch", lambda *args, **kwargs: pytest.fail("held job launched"))
    supervisor.tick(project_root)
    state = dict(conn.execute("SELECT status,last_error FROM jobs WHERE id='feature'").fetchone())
    assert state["status"] == "BLOCKED" and state["last_error"].startswith("budget:")
    assert "151,000" in state["last_error"] and db.get_task(conn, task_id)["status"] == sm.REVIEW
    configure(project_root, budgets={"job_tokens": 500_000})
    monkeypatch.setattr(supervisor, "_control_launch", lambda *args, **kwargs: None)
    supervisor.tick(project_root)
    assert conn.execute("SELECT status FROM jobs WHERE id='feature'").fetchone()[0] == "ACTIVE"


def test_the_manager_reads_compact_job_memory_and_plans_small_jobs_as_one_task(conn, project_root):
    project = configure(project_root, workflow={"mode": "feature", "review": "ai"})
    jobs.create(project_root, "feature", "Bound the retry delay", acceptance=["Delay never exceeds the limit"])
    body = supervisor.coordinator_body(conn, project, jobs.load(project_root, "feature"), resumed=False)
    assert "include_history" in body and "specific unresolved finding" in body
    assert "exactly one task" in workflow.planning_note(project)
