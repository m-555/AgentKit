"""Denied tools remain in history; actual scope violations cannot be waived."""
import pytest

from agentkit import db, guard_denials, host_completion, repo, worktrees


def review_task(conn, project_root, project, monkeypatch):
    identifier = db.create_task(conn, title="denied", spec_id="denied", status="REVIEW",
                                generation=1, owned_paths=["services/retry.py"])
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=repo.head_commit(work))
    monkeypatch.setattr(host_completion, "authority", lambda *args: "test-native-manager")
    return identifier, work


def test_denial_resolution_is_bound_to_current_clean_commit(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    db.record_violation(conn, identifier, "L4", "blocked command", "denied before execution", channel="shell")
    head = repo.head_commit(work)
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (identifier,))]
    task = db.get_task(conn, identifier)
    assert not guard_denials.reviewed(conn, task, rows)
    guard_denials.resolve(conn, project, identifier, head, "Prevented shell; exact clean scope inspected")
    assert guard_denials.reviewed(conn, task, rows)
    assert conn.execute("SELECT count(*) FROM violations WHERE task_id=?", (identifier,)).fetchone()[0] == 1
    (work / "services/retry.py").write_text("later dirty change\n")
    assert not guard_denials.reviewed(conn, task, rows)


def test_post_write_violation_cannot_be_waived(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    db.record_violation(conn, identifier, "L5", "services/retry.py", "scope failed", channel="worktree_audit")
    with pytest.raises(PermissionError, match="pre-execution"):
        guard_denials.resolve(conn, project, identifier, repo.head_commit(work), "Cannot waive")


def test_prevented_blocked_read_can_be_acknowledged_without_erasing_history(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    db.update_task(conn, identifier, status="BLOCKED")
    db.record_violation(conn, identifier, "L4", "compound read", "denied before execution", channel="shell")
    guard_denials.resolve(conn, project, identifier, repo.head_commit(work), "Denied before execution; clean scope inspected")
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (identifier,))]
    assert len(rows) == 1
    assert guard_denials.reviewed(conn, db.get_task(conn, identifier), rows)


def test_exact_denial_evidence_survives_generation_only_retry(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    db.record_violation(conn, identifier, "L4", "read", "denied", channel="shell")
    guard_denials.resolve(conn, project, identifier, repo.head_commit(work), "Prevented read; clean scope")
    db.bump_generation(conn, identifier)
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (identifier,))]
    assert guard_denials.reviewed(conn, db.get_task(conn, identifier), rows)
    db.record_violation(conn, identifier, "L4", "new attempt", "denied", channel="shell")
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (identifier,))]
    assert not guard_denials.reviewed(conn, db.get_task(conn, identifier), rows)


def test_completed_task_denial_review_requires_exact_approval(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    head = repo.head_commit(work)
    db.update_task(conn, identifier, status="DONE", last_commit=head)
    db.record_violation(conn, identifier, "L4", "compound read", "prevented", channel="shell")
    with pytest.raises(ValueError, match="independent PASS"):
        guard_denials.resolve(conn, project, identifier, head, "Review preserved denial")
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (identifier,head,"PASS","independent","Exact scope accepted",db.utcnow()))
    task = db.get_task(conn, identifier)
    db.record_gate(conn, identifier, task['gate_level'], head, True, "passed")
    guard_denials.resolve(conn, project, identifier, head, "Prevented read; exact approved clean commit inspected")
    rows = [dict(r) for r in conn.execute("SELECT * FROM violations WHERE task_id=?", (identifier,))]
    assert guard_denials.reviewed(conn, db.get_task(conn, identifier), rows)
    assert db.get_task(conn, identifier)['status'] == 'DONE'
    assert len(rows) == 1


def test_completed_scope_violation_remains_unwaivable(conn, project_root, project, monkeypatch):
    identifier, work = review_task(conn, project_root, project, monkeypatch)
    head = repo.head_commit(work)
    db.update_task(conn, identifier, status="DONE", last_commit=head)
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (identifier,head,"PASS","independent","Exact scope accepted",db.utcnow()))
    db.record_gate(conn, identifier, db.get_task(conn, identifier)['gate_level'], head, True, "passed")
    db.record_violation(conn, identifier, "L5", "other.py", "post-write scope", channel="worktree_audit")
    with pytest.raises(PermissionError, match="pre-execution"):
        guard_denials.resolve(conn, project, identifier, head, "Cannot waive")
