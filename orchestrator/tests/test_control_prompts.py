"""A reviewer's prompt always fits max_context_chars; a prompt that cannot fit is a problem, not a crash.

The Sonnet benchmark (2026-10-12) stopped here. The final reviewer's packet carried
up to 40,000 characters of diff, the workflow refuses any prompt over
max_context_chars (16,000 by default), and the refusal escaped the supervisor's
pass. The supervisor then retried every five minutes while the job waited forever.
Per-task review packets (15,000 characters of diff) had the same flaw. A packet
now shrinks its diff to fit and says where the rest is. If even that cannot fit,
the launch is refused visibly as a problem the user can answer.
"""
from __future__ import annotations

import sys

import pytest
import yaml

from agentkit import (
    adapters,
    control_prompts,
    db,
    jobs,
    problems,
    processes,
    supervisor,
    workflow,
)
from agentkit import statemachine as sm
from agentkit.adapters.base import Installation, Launch
from agentkit.config import load_project
from tests.conftest import commit_all
from tests.test_infra_stop import job_state, job_task

BIG = "".join(f"VALUE_{n} = {n}  # padding line that makes the change large\n" for n in range(900))


def configure(project_root, review, **extra):
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": "feature", "review": review, **extra}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_project(project_root)


@pytest.fixture
def prompts(monkeypatch):
    """Real prompt assembly and validation; no CLI and no process is started."""
    seen = []
    adapter = type(adapters.get("codex"))
    monkeypatch.setattr(adapter, "detect", lambda self: Installation("codex", sys.executable, "test", source="test"))
    monkeypatch.setattr(adapter, "runtime_problem", lambda self, model: None)
    monkeypatch.setattr(adapter, "build_launch", lambda self, task, work, role, project, prompt, resume_token=None:
                        seen.append(prompt) or Launch([sys.executable], cwd=str(work)))
    monkeypatch.setattr(processes, "start", lambda *args, **kwargs: 99)
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    return seen


def large_change(conn, project_root):
    project, task_id, work, _ = job_task(conn, project_root)
    (work / "services/retry.py").write_text("LIMIT = 30\n" + BIG, encoding="utf-8")
    commit_all(work, "A large change")
    return project, task_id


def test_a_large_task_diff_still_fits_the_reviewer_prompt(conn, project_root, prompts):
    project = configure(project_root, "ai")
    project, task_id = large_change(conn, project_root)
    assert len(BIG) > workflow._limit(project, "max_context_chars")
    supervisor._control_launch(conn, project, jobs.load(project_root, "feature"), "review", db.get_task(conn, task_id))
    (prompt,) = prompts
    assert len(prompt) <= workflow._limit(project, "max_context_chars")
    assert "LIMIT = 30" in prompt and "task_diff(offset=" in prompt


def test_a_large_job_diff_still_fits_the_final_reviewer_prompt(conn, project_root, prompts):
    project = configure(project_root, "once")
    project, task_id = large_change(conn, project_root)
    supervisor.tick(project_root)  # gate approval and merge; the final review starts on a later pass
    assert db.get_task(conn, task_id)["status"] == sm.DONE and prompts == []
    supervisor._control_launch(conn, project, jobs.load(project_root, "feature"), "final_review")
    (prompt,) = prompts
    assert len(prompt) <= workflow._limit(project, "max_context_chars")
    assert "job_review_submit(job_id='feature'" in prompt and "source_read(where='integration'" in prompt


def test_a_final_review_that_cannot_fit_is_a_problem_and_the_supervisor_goes_on(conn, project_root, prompts):
    configure(project_root, "once", max_context_chars=600)
    project, task_id, _, _ = job_task(conn, project_root)
    supervisor.tick(project_root)  # gate approval and merge
    supervisor.tick(project_root)  # the final review cannot start: no exception escapes
    state = job_state(conn)
    assert state["status"] == "BLOCKED" and "could not start" in state["last_error"]
    (row,) = problems.listing(conn)
    assert row["kind"] == "final" and row["options"] == ["accept", "retry", "plan"]
    assert prompts == []


def test_a_task_review_that_cannot_fit_is_a_problem_once(conn, project_root, prompts):
    configure(project_root, "ai", max_context_chars=600)
    project, task_id, _, _ = job_task(conn, project_root)
    supervisor.tick(project_root)
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.REVIEW and task["blocker"].startswith(control_prompts.REVIEW_REFUSED)
    supervisor.tick(project_root)  # not retried on every pass
    assert len(db.recent_events(conn, kind="control_launch_refused", limit=10)) == 1
    (row,) = problems.listing(conn)
    assert row["kind"] == "launch" and row["options"] == ["retry", "skip", "plan"]
    problems.act_locked(conn, load_project(project_root), row["code"], "retry")
    assert not db.get_task(conn, task_id)["blocker"]
