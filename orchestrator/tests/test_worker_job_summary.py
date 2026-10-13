"""Token efficiency #2: workers get a short job summary, never a stale one.

Every worker brief carried the job's full user-request history (about 8k
characters in a pilot project) on every model turn, though each task already carries its
own spec, files and rules. The manager writes a summary of at most 1500
characters stamped with the job revision it covers; workers get it while it is
current, and the full history again as soon as the user changes the job.
"""
from __future__ import annotations

import json

import pytest

from agentkit import briefs, instructions, job_summary, jobs, mcp_manager

JOB = "summary-job"
LONG = "Preserve every existing route, default and export. " * 40


@pytest.fixture
def job(project_root, project):
    project.raw["workflow"] = {"mode": "separate-tasks", "max_context_chars": 20000}
    jobs.create(project_root, JOB, LONG, "claude-code", acceptance=["Combined full gate passes"])
    return project


def _context(project):
    return json.loads(job_summary.worker_context(project, JOB, {"job_id": JOB}))


def test_a_current_summary_replaces_the_request_history(project_root, job):
    job_summary.set_summary(project_root, JOB, "Build creative workflows on existing models; keep old defaults.",
                            author="manager")
    context = _context(job)
    assert context["summary"].startswith("Build creative workflows")
    assert "requests" not in context and context["acceptance"] == ["Combined full gate passes"]
    assert len(json.dumps(context)) < len(LONG)


def test_a_user_change_brings_the_full_history_back(project_root, job):
    job_summary.set_summary(project_root, JOB, "Old summary", author="manager")
    jobs.amend(project_root, JOB, "New instruction: also keep exports byte-identical.", user=True)
    context = _context(job)
    assert "summary" not in context
    assert any("byte-identical" in item["text"] for item in context["requests"])


def test_without_a_summary_workers_keep_the_full_history(job):
    assert _context(job)["requests"][0]["text"] == LONG


def test_briefs_and_prompts_use_the_summary(project_root, job, monkeypatch):
    job_summary.set_summary(project_root, JOB, "Short orientation for workers.", author="manager")
    calls = []
    monkeypatch.setattr(job_summary, "worker_context", lambda *a: calls.append(a) or "{}")
    briefs._job_memory(job, {"job_id": JOB})
    instructions.prompt({"kind": "SAFE_PARALLEL", "role": "implementer", "job_id": JOB, "skills": []}, job, "base")
    assert len(calls) == 2  # the brief and the prompt both go through worker_context


@pytest.mark.parametrize("text", ["", "   ", "x" * (job_summary.LIMIT + 1)])
def test_an_empty_or_long_summary_is_refused(project_root, job, text):
    with pytest.raises(ValueError):
        job_summary.set_summary(project_root, JOB, text, author="manager")


def test_setting_a_summary_is_recorded_as_a_manager_decision(project_root, job):
    job_summary.set_summary(project_root, JOB, "Short orientation.", author="manager")
    stored = jobs.load(project_root, JOB)
    assert stored["worker_summary"]["revision"] == stored["revision"]
    assert "Worker summary" in stored["decisions"][-1]["text"]


def test_a_worker_cannot_write_the_summary(project_root, job, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setenv("AGENTKIT_TASK", "5")
    with pytest.raises(PermissionError):
        mcp_manager.job_worker_summary(JOB, "I decide what the job is.")
