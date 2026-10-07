"""Slow Git and file work never runs while holding the database write lock.

Every monitor heartbeat, hook and MCP call waits at most `db.BUSY_TIMEOUT_MS`
for the write lock. A transaction that spends longer than that on Git work
starves them all, and a monitor that cannot write used to stop its worker.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    db,
    gates,
    integration_retry,
    integrator,
    jobs,
    manager_audit,
    manager_evidence,
    manager_state,
    models,
    providers,
)
from tests.test_workflow import reviewed_change

PIN = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)


def _writable(root) -> bool:
    """Whether another process could take the write lock right now."""
    other = sqlite3.connect(str(db.db_path(root)), timeout=0.2, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


@pytest.fixture
def managed_job(project_root, conn, monkeypatch):
    job = jobs.create(project_root, "locks", "Keep the database responsive", "codex")
    job = jobs.pin_coordinator(project_root, job["id"], PIN)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    process = conn.execute(
        "INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at,requested_model,requested_effort) "
        "VALUES('coordinator','codex',?,'RUNNING',?,'{}',?,'gpt-6.1-sol','xhigh')",
        (job["id"], os.getpid(), db.utcnow())).lastrowid
    monkeypatch.setenv("AGENTKIT_PROCESS", str(process))
    return job


def _outage(conn):
    providers.begin_cooldown(conn, "codex", reason="Codex account allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=2))


def _watch(monkeypatch, module, name, root, seen):
    real = getattr(module, name)

    def watching(*args, **kwargs):
        seen.append(_writable(root))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, watching)


def test_acknowledge_gathers_evidence_without_the_write_lock(project_root, conn, managed_job, monkeypatch):
    _outage(conn)
    providers.clear_cooldown(conn, "codex")
    report = manager_audit.run(conn, project_root, managed_job["id"])
    seen: list[bool] = []
    _watch(monkeypatch, manager_audit, "snapshot", project_root, seen)
    manager_audit.acknowledge(conn, project_root, managed_job["id"], report["epoch"], report["digest"],
                              "Inspected intent, tasks and gates")
    assert seen == [True]
    assert not manager_state.pending(conn, managed_job["id"])


def test_acknowledge_refuses_state_that_changes_while_evidence_is_gathered(
        project_root, conn, managed_job, monkeypatch):
    _outage(conn)
    providers.clear_cooldown(conn, "codex")
    task = db.create_task(conn, title="work", job_id=managed_job["id"], status="READY",
                          expected_write=["services/retry.py"])
    report = manager_audit.run(conn, project_root, managed_job["id"])
    real = manager_audit.snapshot

    def changing(*args, **kwargs):
        result = real(*args, **kwargs)
        other = db.connect(project_root)
        try:
            db.write_checkpoint(other, task, {"next_action": "arrived during verification"}, kind="semantic")
        finally:
            other.close()
        return result

    monkeypatch.setattr(manager_audit, "snapshot", changing)
    with pytest.raises(ValueError, match="evidence changed"):
        manager_audit.acknowledge(conn, project_root, managed_job["id"], report["epoch"], report["digest"],
                                  "Old evidence")
    assert manager_state.pending(conn, managed_job["id"])


def test_acknowledge_checks_manager_availability_before_gathering_evidence(
        project_root, conn, managed_job, monkeypatch):
    _outage(conn)
    report = manager_audit.run(conn, project_root, managed_job["id"])
    calls: list[bool] = []
    _watch(monkeypatch, manager_audit, "snapshot", project_root, calls)
    with pytest.raises(ValueError, match="confirmed availability"):
        manager_audit.acknowledge(conn, project_root, managed_job["id"], report["epoch"], report["digest"],
                                  "Manager still unavailable")
    assert calls == []
    assert manager_state.pending(conn, managed_job["id"])


def test_merge_runs_git_without_the_write_lock(project_root, conn, project, monkeypatch):
    task_id, _work, _head = reviewed_change(conn, project, project_root)
    if db.get_task(conn, task_id)["status"] != "INTEGRATION_READY":
        db.set_status(conn, task_id, "INTEGRATION_READY", actor="reviewer", cause="approved")
    monkeypatch.setattr(gates, "run_gate", lambda project, level, **kwargs: gates.GateResult(level, True))
    real = integrator._git
    seen: list[bool] = []

    def watching(root, args, *rest, **kwargs):
        if args and args[0] == "merge":
            seen.append(_writable(project_root))
        return real(root, args, *rest, **kwargs)

    monkeypatch.setattr(integrator, "_git", watching)
    outcome = integrator.merge_one(conn, project, db.get_task(conn, task_id))
    assert outcome.ok, outcome.summary()
    assert seen == [True]
    assert db.get_task(conn, task_id)["status"] == "DONE"


def test_integration_retry_verifies_without_the_write_lock(project_root, conn, project, monkeypatch):
    task_id, _work, _head = reviewed_change(conn, project, project_root)
    db.set_status(conn, task_id, "FAILED", actor="integrator", cause="environment failed")
    db.update_task(conn, task_id, blocker="combined_gate: missing test dependency")
    seen: list[bool] = []
    _watch(monkeypatch, integrator, "verify", project_root, seen)
    integration_retry.authorize(conn, project, task_id, "Installed missing test dependency")
    assert seen == [True]
    assert db.get_task(conn, task_id)["status"] == "INTEGRATION_READY"


def test_integration_retry_refuses_a_task_that_changed_during_verification(
        project_root, conn, project, monkeypatch):
    task_id, _work, _head = reviewed_change(conn, project, project_root)
    db.set_status(conn, task_id, "FAILED", actor="integrator", cause="environment failed")
    db.update_task(conn, task_id, blocker="combined_gate: missing test dependency")
    real = integrator.verify

    def changing(*args, **kwargs):
        result = real(*args, **kwargs)
        other = db.connect(project_root)
        try:
            db.update_task(other, task_id, blocker="review rejected")
        finally:
            other.close()
        return result

    monkeypatch.setattr(integrator, "verify", changing)
    with pytest.raises(ValueError, match="changed"):
        integration_retry.authorize(conn, project, task_id, "Installed missing test dependency")
    assert db.get_task(conn, task_id)["status"] == "FAILED"


def test_an_open_outage_is_recognised_without_taking_the_write_lock(project_root, conn, managed_job):
    manager_state.record_outage(conn, managed_job["id"], "codex", "manager lease expired")
    epoch = manager_state.state(conn, managed_job["id"])["epoch"]
    other = sqlite3.connect(str(db.db_path(project_root)), timeout=0.2, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("PRAGMA busy_timeout=200")
        assert manager_state.record_outage(conn, managed_job["id"], "codex", "still out") == epoch
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_new_outage_reads_integration_evidence_without_the_write_lock(
        project_root, conn, managed_job, monkeypatch):
    seen: list[bool] = []
    _watch(monkeypatch, manager_evidence, "integration", project_root, seen)
    epoch = manager_state.record_outage(conn, managed_job["id"], "codex", "manager lease expired")
    assert epoch == 1
    assert seen and all(seen)
    assert manager_state.pending(conn, managed_job["id"])
