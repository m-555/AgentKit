"""F24: when the external manager is gone, the job's pinned coordinator may review.

A pilot project's reviews belong to the external manager chat. When that chat stopped at a
usage limit and did not wake, the supervisor spawned the job's coordinator, which
woke after the reset but could not approve finished work, because review_submit
accepts only the attached external session. A coordinator running the same
pinned manager model now stands in, but only while the external lease has
lapsed, only with no pending recovery barrier, and never for its own work.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, external_review, jobs, manager, manager_state, models, reviews

OPUS = models.Profile("opus", "claude-code", "claude-opus-5-5", "high", 2)
JOB = "standin-job"


@pytest.fixture
def standin(project_root, project, conn, monkeypatch):
    for name in ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(name, raising=False)
    jobs.create(project_root, JOB, "Keep building while the manager chat sleeps", "claude-code")
    jobs.pin_coordinator(project_root, JOB, OPUS)
    bridge = subprocess.Popen([sys.executable, "-c", "pass"])
    bridge.wait()  # the chat's process has gone, as after a usage limit
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(manager, "pid_alive", lambda pid: True)
        token = manager.attach(conn, project_root, JOB, "root-chat", bridge.pid, session_ref="root")
    credential = project_root / ".ai" / "runtime" / f"manager-{JOB}.credential"
    credential.parent.mkdir(exist_ok=True)
    credential.write_text(token)
    lapsed = (datetime.now(UTC) - timedelta(hours=8)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (lapsed, JOB))
    task_id = db.create_task(conn, title="builder change", status="REVIEW")
    db.update_task(conn, task_id, job_id=JOB)
    process = conn.execute(
        "INSERT INTO processes(purpose,provider,job_id,status,launch_json,started_at,requested_model,requested_effort) "
        "VALUES('coordinator','claude-code',?,'RUNNING','{}',?,'claude-opus-5-5','high')",
        (JOB, db.utcnow())).lastrowid
    project.raw["review_mode"] = "external-manager"
    monkeypatch.setenv("AGENTKIT_PROCESS", str(process))
    calls = []
    monkeypatch.setattr(reviews, "approve", lambda *args: calls.append(args))
    return task_id, process, calls


def test_the_pinned_coordinator_reviews_while_the_external_manager_is_gone(project, conn, standin):
    task_id, process, calls = standin
    message = external_review.submit_as_standin(conn, project, task_id, "exact-head", "pass", "Read the diff")
    assert "standing in" in message
    assert calls[0][2:5] == (task_id, "exact-head", "PASS")
    assert calls[0][5] == f"coordinator-standin:process-{process}"
    assert db.recent_events(conn, task_id, kind="external_manager_standin")


def test_a_live_external_manager_keeps_its_reviews(project, conn, standin, monkeypatch):
    task_id, _, calls = standin
    conn.execute("UPDATE manager_leases SET heartbeat_at=?, pid=? WHERE job_id=?", (db.utcnow(), os.getpid(), JOB))
    with pytest.raises(PermissionError, match="attached external manager"):
        external_review.submit_as_standin(conn, project, task_id, "exact-head", "PASS", "Evidence")
    assert not calls


def test_a_coordinator_on_another_model_cannot_stand_in(project, conn, standin):
    task_id, process, calls = standin
    conn.execute("UPDATE processes SET requested_model='claude-sonnet-5-5' WHERE id=?", (process,))
    with pytest.raises(PermissionError):
        external_review.submit_as_standin(conn, project, task_id, "exact-head", "PASS", "Evidence")
    assert not calls


def test_a_pending_recovery_barrier_comes_first(project, conn, standin):
    task_id, _, calls = standin
    manager_state.record_outage(conn, JOB, "claude-code", "worker quota event")
    with pytest.raises(PermissionError, match="recovery"):
        external_review.submit_as_standin(conn, project, task_id, "exact-head", "PASS", "Evidence")
    assert not calls


def test_a_worker_cannot_stand_in(project, conn, standin, monkeypatch):
    task_id, _, calls = standin
    monkeypatch.setenv("AGENTKIT_TASK", str(task_id))
    with pytest.raises(PermissionError):
        external_review.submit_as_standin(conn, project, task_id, "exact-head", "PASS", "Evidence")
    assert not calls
