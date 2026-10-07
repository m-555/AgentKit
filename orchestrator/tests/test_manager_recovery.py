"""Durable manager epochs, digest authority and same-tick recovery barriers."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    db,
    integrator,
    jobs,
    manager_audit,
    manager_state,
    models,
    processes,
    providers,
    scheduler,
    supervisor,
)
from agentkit.capabilities import CapabilitySet, save_cache
from tests.conftest import git

PIN = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)


@pytest.fixture
def job_state(project_root, conn, monkeypatch):
    job = jobs.create(project_root, "recovery", "Preserve original intent", "codex")
    job = jobs.pin_coordinator(project_root, job["id"], PIN)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    process = conn.execute("INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at,requested_model,requested_effort) VALUES('coordinator','codex',?,'RUNNING',?,'{}',?,'gpt-6.1-sol','xhigh')", (job["id"], os.getpid(), db.utcnow())).lastrowid
    monkeypatch.setenv("AGENTKIT_PROCESS", str(process))
    return job, process


def outage(conn):
    providers.begin_cooldown(conn, "codex", reason="Codex account allowance exhausted", retry_at=datetime.now(UTC) + timedelta(hours=2))


def test_capture_is_atomic_and_precedes_complete_account_refresh(project_root, conn, job_state):
    job, _ = job_state
    outage(conn)
    assert manager_state.pending(conn, job["id"])
    providers.observe(conn, "codex", {"available": True, "complete": True})
    assert providers.is_available(conn, "codex")
    assert manager_state.state(conn, job["id"])["epoch"] == 1
    restarted = db.connect(project_root)
    try:
        assert manager_state.pending(restarted, job["id"])
    finally:
        restarted.close()


def test_ack_requires_current_successful_audit_and_preserves_intent(project_root, conn, job_state):
    job, _ = job_state
    outage(conn)
    providers.clear_cooldown(conn, "codex")
    report = manager_audit.run(conn, project_root, job["id"])
    assert report["passed"]
    conn.execute("UPDATE processes SET heartbeat_at=? WHERE id=?", (db.utcnow(), job_state[1]))
    db.log_event(conn, None, "arbitrary_manager_log", cause="no authoritative change")
    manager_audit.acknowledge(conn, project_root, job["id"], report["epoch"], report["digest"], "Inspected intent, commits, tasks and gates")
    assert not manager_state.pending(conn, job["id"])
    assert jobs.load(project_root, job["id"])["requests"] == job["requests"]


@pytest.mark.parametrize("drift", ["request", "contract", "integration", "task", "gate", "review", "checkpoint"])
def test_recovery_ack_rejects_changed_authoritative_evidence(project_root, conn, job_state, drift):
    job, _ = job_state
    outage(conn)
    providers.clear_cooldown(conn, "codex")
    task = db.create_task(conn, title="work", job_id=job["id"], status="READY", expected_write=["services/retry.py"])
    report = manager_audit.run(conn, project_root, job["id"])
    if drift == "request":
        jobs.amend(project_root, job["id"], "New user condition", user=True)
    elif drift == "contract":
        (project_root / "contracts/api.yaml").write_text("version: 2\n")
    elif drift == "integration":
        git(project_root, "branch", "integration")
    elif drift == "task":
        db.update_task(conn, task, description="changed scope")
    elif drift == "gate":
        db.record_gate(conn, task, "fast", "different", False, "failed")
    elif drift == "review":
        conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,'different','PASS','reviewer','changed',?)", (task, db.utcnow()))
    else:
        db.write_checkpoint(conn, task, {"next_action": "new"}, kind="semantic")
    with pytest.raises(ValueError, match="evidence changed"):
        manager_audit.acknowledge(conn, project_root, job["id"], report["epoch"], report["digest"], "Old evidence")
    assert manager_state.pending(conn, job["id"])


def test_failed_task_and_violation_cannot_be_greenwashed(project_root, conn, job_state):
    job, _ = job_state
    task = db.create_task(conn, title="failed", job_id=job["id"], status="FAILED")
    conn.execute("INSERT INTO violations(task_id,layer,path,reason,created_at) VALUES(?,'L5','other.py','scope escape',?)", (task, db.utcnow()))
    outage(conn)
    providers.clear_cooldown(conn, "codex")
    report = manager_audit.run(conn, project_root, job["id"])
    assert not report["passed"] and any("FAILED" in text for text in report["findings"])
    with pytest.raises(ValueError, match="audit failed"):
        manager_audit.acknowledge(conn, project_root, job["id"], report["epoch"], report["digest"], "Ignore failure")
    assert db.get_task(conn, task)["status"] == "FAILED"


@pytest.mark.parametrize("role", ["review", "worker"])
def test_cross_role_cross_job_and_stale_control_ack_are_rejected(project_root, conn, job_state, role):
    job, process = job_state
    outage(conn)
    providers.clear_cooldown(conn, "codex")
    report = manager_audit.run(conn, project_root, job["id"])
    processes.update(conn, process, purpose=role)
    with pytest.raises(PermissionError):
        manager_audit.acknowledge(conn, project_root, job["id"], report["epoch"], report["digest"], "wrong authority")
    processes.update(conn, process, purpose="coordinator", job_id="different-job")
    with pytest.raises(PermissionError):
        manager_audit.run(conn, project_root, job["id"])
    processes.update(conn, process, job_id=job["id"], status="FINISHED")
    with pytest.raises(PermissionError):
        manager_audit.run(conn, project_root, job["id"])


def test_same_tick_availability_does_not_integrate_before_audit(project_root, conn, job_state, monkeypatch):
    job, _ = job_state
    task = db.create_task(conn, title="ready to merge", job_id=job["id"], status="INTEGRATION_READY")
    outage(conn)
    calls = []
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda connection, *args: providers.observe(connection, "codex", {"available": True, "complete": True}))
    monkeypatch.setattr(integrator, "merge_one", lambda *args: calls.append(args))
    supervisor.tick(project_root)
    assert calls == [] and db.get_task(conn, task)["status"] == "INTEGRATION_READY"
    with pytest.raises(ValueError, match="recovery audit"):
        supervisor.accept_job(project_root, job["id"], job["revision"], "premature")


def test_authorized_independent_work_survives_but_new_dependent_work_waits(project_root, conn, project, job_state, monkeypatch):
    job, _ = job_state
    ready = db.create_task(conn, title="already authorized", job_id=job["id"], status="READY", expected_write=["services/retry.py"])
    failed = db.create_task(conn, title="failed while away", job_id=job["id"], status="FAILED")
    outage(conn)
    new = db.create_task(conn, title="new dependent", job_id=job["id"], kind="DEPENDENT", status="READY", expected_write=["routes/video.py"])
    assert manager_state.allows_launch(conn, db.get_task(conn, ready))
    assert not manager_state.allows_launch(conn, db.get_task(conn, new))
    caps = CapabilitySet(adapter="claude-code")
    for key in caps.values:
        caps.set(key, True)
    save_cache(project_root, {"claude-code": caps})
    plans, _ = scheduler.plan(conn, project_root, project)
    assert [p.task["id"] for p in plans] == [ready]
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    supervisor.tick(project_root)
    assert db.get_task(conn, failed)["status"] == "FAILED"


def test_dependency_or_request_drift_revokes_preoutage_launch(project_root, conn, job_state):
    job, _ = job_state
    dependency = db.create_task(conn, title="dependency", job_id=job["id"], status="DONE", last_commit="first")
    task = db.create_task(conn, title="dependent", job_id=job["id"], status="READY", depends_on=[dependency])
    outage(conn)
    assert manager_state.allows_launch(conn, db.get_task(conn, task))
    db.update_task(conn, dependency, last_commit="different")
    assert not manager_state.allows_launch(conn, db.get_task(conn, task))
    db.update_task(conn, dependency, last_commit="first")
    jobs.amend(project_root, job["id"], "user correction", user=True)
    assert not manager_state.allows_launch(conn, db.get_task(conn, task))


def test_weekly_window_and_unknown_reset_keep_manager_unavailable(project_root, conn, job_state):
    job, _ = job_state
    providers.observe(conn, "codex", {"available": False, "reason": "unknown allowance reset"})
    assert providers.get_state(conn, "codex").retry_at and manager_state.pending(conn, job["id"])
    now = datetime.now(UTC)
    providers.observe(conn, "codex", {"available": True, "windows": [
        {"window": "five_hour", "used_percent": 100, "resets_at": (now - timedelta(minutes=1)).isoformat()},
        {"window": "weekly", "used_percent": 100, "resets_at": (now + timedelta(days=2)).isoformat()}]})
    assert not providers.is_available(conn, "codex")
    assert providers.get_state(conn, "codex").retry_at >= (now + timedelta(days=1)).isoformat()


def test_event_logging_does_not_commit_an_explicit_owner_transaction(conn):
    with pytest.raises(RuntimeError):
        with db.immediate_transaction(conn):
            db.log_event(conn, None, "transaction-test")
            assert conn.in_transaction
            raise RuntimeError("abort owner claim")
    assert not db.recent_events(conn, kind="transaction-test")


@pytest.mark.parametrize("status", ["CANCELLED", "BLOCKED"])
def test_quarantine_preserves_dirty_evidence_without_accepting_work(project_root, conn, job_state, status):
    from agentkit import repo, worktrees
    from agentkit.config import load_project
    job, _ = job_state
    identifier = db.create_task(conn, title="Preserved draft", job_id=job["id"], status=status,
                                expected_write=["services/draft.py"])
    db.create_task(conn, title="Corrective owner", job_id=job["id"], status="READY",
                   expected_write=["services/draft.py"], owned_paths=["services/draft.py"])
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, load_project(project_root))
    db.update_task(conn, identifier, worktree=str(work), base_sha=repo.head_commit(work),
                   branch=repo.current_branch(work))
    (work / "services/draft.py").write_text("DRAFT = 1\n")
    db.record_violation(conn, identifier, "L4", "sed attempted", "Denied command", channel="shell")
    report = manager_audit.run(conn, project_root, job["id"])
    saved = next(row for row in report["report"]["tasks"] if row["id"] == identifier)
    assert saved["violations"] and saved["dirty_digest"]
    assert not saved["scope_audit"]["clean"]
    assert (work / "services/draft.py").read_text() == "DRAFT = 1\n"
    assert report["passed"] is (status == "CANCELLED")
    assert identifier not in [row["id"] for row in integrator.queue(conn)]


def test_cancelled_task_with_live_owner_still_blocks_recovery(project_root, conn, job_state):
    job, _ = job_state
    identifier = db.create_task(conn, title="Not stopped", job_id=job["id"], status="CANCELLED")
    conn.execute("INSERT INTO processes(purpose,provider,task_id,status,pid,launch_json,started_at) "
                 "VALUES('worker','codex',?,'RUNNING',?,'{}',?)", (identifier, os.getpid(), db.utcnow()))
    report = manager_audit.run(conn, project_root, job["id"])
    assert not report["passed"]
    assert any("ownership is not stopped" in finding for finding in report["findings"])
