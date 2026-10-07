"""Preserve branches and exact approval across restart/database loss."""
import pytest

from agentkit import db, integrator, quota, repo, reviews, workspace_registry, worktrees
from tests.conftest import commit_all, git


def test_legacy_database_recovers_quota_without_losing_task(conn, project_root):
    task_id = db.create_task(conn, title="legacy running worker", status="RUNNING", owned_paths=["services/retry.py"])
    conn.execute("ALTER TABLE tasks DROP COLUMN blocked_meta")
    upgraded = db.connect(project_root)
    try:
        quota.pause(upgraded, task_id, provider="claude-code", retry_at=None)
        task = db.get_task(upgraded, task_id)
        assert task["status"] == "BLOCKED" and quota.is_quota_paused(task)
        assert task["attempts"] == 0
    finally:
        upgraded.close()
    again = db.connect(project_root)
    assert quota.is_quota_paused(db.get_task(again, task_id))
    again.close()


def test_colliding_long_spec_ids_get_distinct_paths(project_root):
    first = {"id": 1, "spec_id": "x" * 60 + "a"}
    second = {"id": 2, "spec_id": "x" * 60 + "b"}
    assert worktrees.path_for(project_root, first) != worktrees.path_for(project_root, second)
    assert worktrees.branch_name(first) != worktrees.branch_name(second)


def test_manifest_survives_database_rebuild(conn, project_root, project):
    task_id = db.create_task(conn, title="recover", spec_id="stable", owned_paths=["services/retry.py"])
    task = db.get_task(conn, task_id)
    work, created = worktrees.ensure(project_root, task, project)
    assert created
    changed = work / "services/retry.py"
    changed.write_text("VALUE = 'unfinished'\n")
    rebuilt = {"id": task_id + 100, "spec_id": "stable"}
    assert worktrees.ensure(project_root, rebuilt, project) == (work, False)
    assert changed.read_text() == "VALUE = 'unfinished'\n"
    report = workspace_registry.inventory(project_root, [])
    assert report[0]["branch_exists"] and report[0]["path"] == str(work)


def test_wrong_existing_checkout_is_refused(conn, project_root, project):
    task = db.get_task(conn, db.create_task(conn, title="scope", spec_id="scope"))
    work, _ = worktrees.ensure(project_root, task, project)
    git(work, "switch", "-c", "unexpected")
    with pytest.raises(ValueError, match="branch differs"):
        worktrees.ensure(project_root, task, project)


def test_orphan_branch_is_discoverable_after_manifest_loss(conn, project_root, project):
    task = db.get_task(conn, db.create_task(conn, title="orphan", spec_id="orphan"))
    work, _ = worktrees.ensure(project_root, task, project)
    workspace_registry.manifest(project_root).unlink()
    rows = workspace_registry.inventory(project_root, [])
    assert any(row["phase"] == "unregistered" and row["checkout"] == str(work) for row in rows)


@pytest.mark.parametrize("replacement", ["two", "", None])
def test_registry_never_redirects_a_reservation(project_root, replacement):
    task = {"id": 1, "spec_id": "fixed"}
    workspace_registry.record(project_root, task, path="one", branch="agent/one")
    with pytest.raises(ValueError, match="silently change"):
        workspace_registry.record(project_root, task, path=replacement)


def test_interrupted_integration_rechecks_approval_and_full_gate(conn, project_root, project):
    task_id = db.create_task(conn, title="merge", spec_id="merge", status="REVIEW", owned_paths=["services/retry.py"])
    task = db.get_task(conn, task_id)
    work, _ = worktrees.ensure(project_root, task, project)
    base = repo.head_commit(work)
    (work / "services/retry.py").write_text("VALUE = 'accepted'\n")
    head = commit_all(work, "implement retry")
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    db.record_gate(conn, task_id, "fast", head, True, "pass")
    reviews.approve(conn, project, task_id, head, "PASS", "independent-reviewer", "Inspected retry change and boundary")
    db.update_task(conn, task_id, status="INTEGRATING", blocker="interrupted integration")
    outcome = integrator.merge_one(conn, project, db.get_task(conn, task_id))
    assert outcome.ok, outcome.summary()
    assert db.get_task(conn, task_id)["status"] == "DONE"
    assert repo.is_ancestor(project_root, head, "integration")
    assert repo.head_commit(project_root) == base
    saved = workspace_registry.stored(project_root, task)
    assert saved["phase"] == "merged" and saved["integration_branch"] == "integration"


def test_untrusted_spec_id_cannot_escape_fixed_sibling_path(project_root):
    task = {"id": 42, "spec_id": "x\\..\\..\\escape"}
    path = worktrees.path_for(project_root, task)
    assert path.parent.resolve() == (project_root.parent / "wt-demo").resolve()
    assert ".." not in worktrees.branch_name(task)


def test_transient_windows_manifest_reader_does_not_lose_reservation(project_root, monkeypatch):
    from agentkit import locking
    original = locking.os.replace
    attempts = []
    def busy_then_replace(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            error = PermissionError("reader has the target open")
            error.winerror = 32
            raise error
        return original(source, target)
    monkeypatch.setattr(locking.os, "replace", busy_then_replace)
    monkeypatch.setattr(locking.time, "sleep", lambda seconds: None)
    task = {"id": 1, "spec_id": "stable-write"}
    workspace_registry.record(project_root, task, path="fixed", branch="agent/fixed")
    assert len(attempts) == 3
    assert workspace_registry.stored(project_root, task)["branch"] == "agent/fixed"
