"""Completed history survives a later lease without allowing new unapproved edits."""
from __future__ import annotations

import pytest

from agentkit import db, jobs, manager_audit, models, repo
from tests.conftest import commit_all, git


@pytest.fixture
def completed(project_root, conn):
    job = jobs.create(project_root, "history", "Keep exact completed evidence", "codex")
    jobs.pin_coordinator(project_root, job["id"], models.PROFILES["sol"])
    commit_all(project_root, "job definition")
    base = repo.head_commit(project_root)
    (project_root / "services/media.py").write_text("VALUE = 'completed'\n")
    head = commit_all(project_root, "old task complete")
    assert head != base
    assert git(project_root, "branch", "-f", "integration", head).returncode == 0
    identifier = db.create_task(conn, title="old task", status="DONE", job_id=job["id"],
                                owned_paths=["services/media.py"])
    db.update_task(conn, identifier, worktree=str(project_root), branch=repo.current_branch(project_root),
                   base_sha=base, last_commit=head, gate_level="fast")
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) "
                 "VALUES(?,?,?,?,?,?)", (identifier, head, "PASS", "independent", "exact commit", db.utcnow()))
    db.record_gate(conn, identifier, "fast", head, True, "passed")
    db.create_task(conn, title="later writer", status="RUNNING", owned_paths=["services/media.py"])
    return job, identifier


def test_unchanged_done_commit_ignores_later_lease_for_its_declared_file(project_root, conn, completed):
    job, identifier = completed
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert report["passed"], report["findings"]
    task = next(t for t in report["tasks"] if t["id"] == identifier)
    assert task["scope_audit"]["clean"]


@pytest.mark.parametrize("change", ["dirty", "unapproved_commit", "scope_narrowed", "still_reviewing"])
def test_historical_exception_never_hides_tampering_or_live_review(project_root, conn, completed, change):
    job, identifier = completed
    if change == "dirty":
        (project_root / "services/media.py").write_text("VALUE = 'dirty'\n")
    elif change == "unapproved_commit":
        (project_root / "services/media.py").write_text("VALUE = 'unreviewed'\n")
        commit_all(project_root, "new unapproved commit")
    elif change == "scope_narrowed":
        db.update_task(conn, identifier, owned_paths=["services/retry.py"])
    else:
        db.update_task(conn, identifier, status="REVIEW")
    report = manager_audit.snapshot(conn, project_root, job["id"])
    assert not report["passed"]
    assert any(f"task {identifier}:" in finding for finding in report["findings"])
