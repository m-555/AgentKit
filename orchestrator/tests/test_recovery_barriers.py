"""Reviewer regressions for blocked locks, long gates and missing audit proof."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agentkit import (
    db,
    gates,
    integrator,
    jobs,
    manager_audit,
    manager_evidence,
    manager_state,
    models,
    providers,
    repo,
    supervisor,
)
from agentkit.locking import exclusive
from tests.conftest import commit_all, git

PIN = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)


def manager_job(root, conn):
    job = jobs.create(root, "barrier", "Review every recovery epoch", "codex")
    jobs.pin_coordinator(root, job["id"], PIN)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    return job


def test_outage_while_waiting_for_integration_lock_blocks_merge(project_root, conn, project, monkeypatch):
    job = manager_job(project_root, conn)
    task = db.create_task(conn, title="merge", status="INTEGRATION_READY", job_id=job["id"])
    waiting = threading.Event()
    original = integrator.exclusive
    def lock(*args, **kwargs):
        waiting.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(integrator, "exclusive", lock)
    monkeypatch.setattr(integrator, "_merge_one", lambda *args, **kwargs: pytest.fail("merged without recovery audit"))
    def attempt():
        connection = db.connect(project_root)
        try:
            return integrator.merge_one(connection, project, db.get_task(connection, task))
        finally:
            connection.close()
    with ThreadPoolExecutor(1) as pool:
        with exclusive(project_root, "integration"):
            pending = pool.submit(attempt)
            assert waiting.wait(5)
            providers.begin_cooldown(conn, "codex", reason="actual quota outage")
        result = pending.result(timeout=10)
    assert not result.ok and result.stage == "manager_recovery"
    assert db.get_task(conn, task)["status"] == "INTEGRATION_READY"


def test_outage_during_acceptance_gate_cannot_mark_job_done(project_root, conn, monkeypatch):
    job = manager_job(project_root, conn)
    task = db.create_task(conn, title="completed", status="DONE", job_id=job["id"])
    work = integrator.integration_worktree(__import__("agentkit.config", fromlist=["load_project"]).load_project(project_root))
    head = repo.head_commit(work)
    # Reach the combined gate with an independently approved task commit.
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) "
                 "VALUES(?,?,?,?,?,?)", (task, head, "PASS", "independent-reviewer", "task approved", db.utcnow()))
    def gate(*args, **kwargs):
        providers.begin_cooldown(conn, "codex", reason="manager quota during full gate")
        return SimpleNamespace(passed=True, summary=lambda: "full passed")
    monkeypatch.setattr(gates, "run_gate", gate)
    with pytest.raises(ValueError, match="recovery audit"):
        supervisor.accept_job(project_root, job["id"], job["revision"], "premature acceptance")
    state = conn.execute("SELECT status,completed_sha FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert state["status"] != "DONE" and state["completed_sha"] is None
    assert repo.head_commit(work) == head


@pytest.mark.parametrize("status", ["DONE", "INTEGRATION_READY", "REVIEW", "INTEGRATING"])
def test_progressed_task_without_worktree_base_or_branch_fails_audit(project_root, conn, status):
    job = manager_job(project_root, conn)
    db.create_task(conn, title="missing evidence", status=status, job_id=job["id"])
    manager_state.record_outage(conn, job["id"], "codex", "recover")
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert not report["passed"] and any("missing worktree/base/branch" in text for text in report["findings"])


def test_integration_commits_exclude_independent_operator_commits(project_root):
    base = repo.head_commit(project_root)
    git(project_root, "branch", "integration")
    (project_root / "services/retry.py").write_text("VALUE = 'operator only'\n")
    commit_all(project_root, "operator-only commit")
    result = manager_evidence.integration(project_root, base)
    assert result["head"] == base and result["commits"] == []


def test_invalid_worker_assignment_does_not_erase_manager_outage(project_root, conn):
    job = manager_job(project_root, conn)
    task = db.create_task(conn, title="invalid worker policy", status="READY", model_assignment="removed", job_id=job["id"])
    providers.observe(conn, "codex", {"available": False, "reason": "actual account quota"})
    assert not providers.is_available(conn, "codex") and manager_state.pending(conn, job["id"])
    from agentkit import models
    from agentkit.config import load_project
    with pytest.raises(ValueError, match="unknown model assignment"):
        models.allowed_workers(conn, load_project(project_root), db.get_task(conn, task))


def test_ack_authority_is_revalidated_after_wait_for_database_lock(project_root, conn, monkeypatch):
    job = manager_job(project_root, conn)
    pid = conn.execute("INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at,requested_model,requested_effort) VALUES('coordinator','codex',?,'RUNNING',?,'{}',?,'gpt-6.1-sol','xhigh')", (job["id"], os.getpid(), db.utcnow())).lastrowid
    monkeypatch.setenv("AGENTKIT_PROCESS", str(pid))
    manager_state.record_outage(conn, job["id"], "codex", "recover")
    report = manager_audit.run(conn, project_root, job["id"])
    entered = threading.Event()
    ready = threading.Event()
    begin = threading.Event()
    original = db.immediate_transaction
    def transaction(connection, **kwargs):
        entered.set()
        return original(connection, **kwargs)
    monkeypatch.setattr(db, "immediate_transaction", transaction)
    def acknowledge():
        connection = db.connect(project_root)
        ready.set()
        try:
            assert begin.wait(5)
            manager_audit.acknowledge(connection, project_root, job["id"], report["epoch"], report["digest"], "outdated owner")
        finally:
            connection.close()
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(acknowledge)
        assert ready.wait(5)
        with original(conn):
            begin.set()
            assert entered.wait(5)
            conn.execute("UPDATE processes SET status='FINISHED' WHERE id=?", (pid,))
        with pytest.raises(PermissionError):
            pending.result(timeout=10)
    assert manager_state.pending(conn, job["id"])


def test_paused_integration_requires_exact_review_and_task_gate(project_root, conn):
    job = manager_job(project_root, conn)
    commit_all(project_root, "record job before paused integration")
    head = repo.head_commit(project_root)
    identifier = db.create_task(conn, title="paused integration", status="INTEGRATING",
                                job_id=job["id"], worktree=str(project_root),
                                branch="main", base_sha=head, blocker="manager recovery pending")
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert not report["passed"] and any("independent PASS review" in item for item in report["findings"])
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,'PASS','independent','reviewed',?)", (identifier, head, db.utcnow()))
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert not report["passed"] and any("passing task gate" in item for item in report["findings"])
    db.record_gate(conn, identifier, "fast", head, True, "verified current worker commit")
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert report["passed"], report["findings"]
