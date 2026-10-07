"""Operator re-pin of a job's manager to another provider.

The coordinator pin never changes on its own. When the user appoints a
different manager, for example because the pinned account is out for days,
the operator records that decision. Intent, tasks and evidence are preserved,
the previous manager's lease stops working, and the new manager must audit and
acknowledge a fresh recovery epoch before integration resumes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    db,
    jobs,
    manager,
    manager_audit,
    manager_repin,
    manager_state,
    models,
    providers,
)

SOL = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)
EVIDENCE = "User appointed the Claude chat as manager; the Codex account is out for days."


@pytest.fixture
def codex_job(project_root, conn, monkeypatch):
    for name in ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(name, raising=False)
    job = jobs.create(project_root, "repin", "Keep building while the manager changes", "codex")
    job = jobs.pin_coordinator(project_root, job["id"], SOL)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    providers.begin_cooldown(conn, "codex", reason="Codex account allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(days=5))
    assert manager_state.pending(conn, job["id"])
    return job


def _old_lease(conn, job_id, *, heartbeat, pid):
    conn.execute(
        "INSERT INTO manager_leases(job_id,holder,token_hash,provider,model,effort,session_ref,pid,ttl_seconds,acquired_at,heartbeat_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, "old-root", "0" * 64, "codex", "gpt-6.1-sol", "xhigh", "old-thread", pid, 300,
         db.utcnow(), heartbeat))


def _hours_ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def test_repin_moves_the_pin_retires_the_old_lease_and_opens_a_fresh_epoch(project_root, conn, codex_job):
    _old_lease(conn, codex_job["id"], heartbeat=_hours_ago(8), pid=os.getpid())
    before = manager_state.state(conn, codex_job["id"])["epoch"]
    result = manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    job = jobs.load(project_root, codex_job["id"])
    assert job["coordinator"] == "claude-code"
    assert job["coordinator_model"]["provider"] == "claude-code"
    assert job["coordinator_model"]["model"] == models.PROFILES["opus"].model
    assert job["requests"] == codex_job["requests"]
    assert job["revision"] == codex_job["revision"]
    assert EVIDENCE in job["decisions"][-1]["text"]
    assert manager.lease(conn, codex_job["id"]) is None
    state = manager_state.state(conn, codex_job["id"])
    assert state["epoch"] == before + 1 and manager_state.pending(conn, codex_job["id"])
    assert result["epoch"] == state["epoch"]
    event = db.recent_events(conn, kind="manager_repinned", limit=1)[0]
    assert event["detail"]["previous_lease"]["holder"] == "old-root"
    assert "token_hash" not in event["detail"]["previous_lease"]


def test_repin_refuses_while_the_old_manager_lease_is_fresh(project_root, conn, codex_job):
    _old_lease(conn, codex_job["id"], heartbeat=db.utcnow(), pid=os.getpid())
    with pytest.raises(PermissionError, match="still holds"):
        manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    assert jobs.load(project_root, codex_job["id"])["coordinator_model"]["provider"] == "codex"
    assert manager.lease(conn, codex_job["id"])["holder"] == "old-root"


def test_repin_refuses_while_a_coordinator_process_owns_the_job(project_root, conn, codex_job):
    conn.execute("INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at) "
                 "VALUES('coordinator','codex',?,'RUNNING',?,'{}',?)", (codex_job["id"], os.getpid(), db.utcnow()))
    with pytest.raises(PermissionError, match="coordinator"):
        manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    assert jobs.load(project_root, codex_job["id"])["coordinator_model"]["provider"] == "codex"


@pytest.mark.parametrize("profile", ["sonnet", "qwen", "missing"])
def test_repin_accepts_only_control_profiles(project_root, conn, codex_job, profile):
    with pytest.raises(ValueError):
        manager_repin.repin(conn, project_root, codex_job["id"], profile, EVIDENCE)


def test_repin_needs_evidence_and_a_different_pin(project_root, conn, codex_job):
    with pytest.raises(ValueError, match="evidence"):
        manager_repin.repin(conn, project_root, codex_job["id"], "opus", "  ")
    manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    with pytest.raises(ValueError, match="already"):
        manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)


@pytest.mark.parametrize("variable", ["AGENTKIT_TASK", "AGENTKIT_PROCESS"])
def test_a_worker_or_coordinator_cannot_repin(project_root, conn, codex_job, monkeypatch, variable):
    monkeypatch.setenv(variable, "7")
    with pytest.raises(PermissionError, match="worker or coordinator"):
        manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    assert jobs.load(project_root, codex_job["id"])["coordinator_model"]["provider"] == "codex"


def test_the_new_manager_attaches_audits_and_acknowledges(project_root, conn, codex_job, monkeypatch):
    _old_lease(conn, codex_job["id"], heartbeat=_hours_ago(8), pid=os.getpid())
    manager_repin.repin(conn, project_root, codex_job["id"], "opus", EVIDENCE)
    bridge = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "new-manager-session")
        token = manager.attach(conn, project_root, codex_job["id"], "claude-root", bridge.pid,
                               ttl_seconds=600, session_ref="new-manager-session")
        report = manager_audit.run(conn, project_root, codex_job["id"], token=token)
        assert report["passed"], report["findings"]
        manager_audit.acknowledge(conn, project_root, codex_job["id"], report["epoch"], report["digest"],
                                  "Audited intent, tasks and evidence after the re-pin", token=token)
        assert not manager_state.pending(conn, codex_job["id"])
    finally:
        bridge.kill()
        bridge.wait()
