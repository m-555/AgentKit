"""Carry independent committed tests without a second model or lost branch."""
import pytest
from conftest import git

from agentkit import db, host_completion, manager_state, repo, test_refresh, worktrees


def prepare(conn, project_root, project, monkeypatch):
    monkeypatch.setattr(host_completion, "authority", lambda *a: "manager")
    monkeypatch.setattr(manager_state, "pending", lambda *a: False)
    identifier = db.create_task(conn, title="test", spec_id="test", kind="TEST_ONLY", status="BLOCKED", owned_paths=["tests/carried.py"])
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    old_base = repo.head_commit(work)
    (work / "tests/carried.py").write_text("def test_independent():\n    assert True\n")
    git(work, "add", "tests/carried.py")
    git(work, "commit", "-qm", "independent test")
    old = repo.head_commit(work)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=old_base, last_commit=old)
    # Update the integration branch in its own ordinary checkout.
    branch = test_refresh.ensure_integration_branch(project)
    integration = project_root.parent / "accepted-source"
    git(project_root, "worktree", "add", str(integration), branch)
    (integration / "services/retry.py").write_text("VALUE = 'corrected'\n")
    git(integration, "add", "services/retry.py")
    git(integration, "commit", "-qm", "accepted source")
    source = db.create_task(conn, title="source", spec_id="source", status="DONE")
    db.update_task(conn, source, last_commit=repo.head_commit(integration))
    return identifier, source, work, old


def test_refresh_preserves_assertions_and_old_branch_then_gates(conn, project_root, project, monkeypatch):
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    original = (work / "tests/carried.py").read_bytes()
    result = test_refresh.refresh(conn, project, identifier, source)
    assert result["passed"] and db.get_task(conn, identifier)["status"] == "REVIEW"
    assert (work / "tests/carried.py").read_bytes() == original
    assert "corrected" in (work / "services/retry.py").read_text()
    assert git(work, "rev-parse", result["backup"]).stdout.strip() == old
    assert db.cached_gate(conn, identifier, "fast", result["head"])["passed"]
    snapshot = db.latest_checkpoint(conn, identifier, kind="mechanical")
    assert snapshot and snapshot["payload"]["head_sha"] == result["head"]


def test_refresh_refuses_unaccepted_source_without_rewriting(conn, project_root, project, monkeypatch):
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    db.update_task(conn, source, status="REVIEW")
    with pytest.raises(ValueError, match="integrated"):
        test_refresh.refresh(conn, project, identifier, source)
    assert repo.head_commit(work) == old


def test_dirty_held_tests_are_preserved_without_rebase(conn, project_root, project, monkeypatch):
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    (work / "tests/carried.py").write_text("dirty preserved\n")
    with pytest.raises(ValueError, match="clean"):
        test_refresh.refresh(conn, project, identifier, source)
    assert repo.head_commit(work) == old and "dirty" in (work / "tests/carried.py").read_text()


def test_conflict_keeps_old_commit_and_backup(conn, project_root, project, monkeypatch):
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    integration = project_root.parent / "accepted-source"
    (integration / "tests/carried.py").write_text("conflicting integration test\n")
    git(integration, "add", "tests/carried.py")
    git(integration, "commit", "-qm", "conflicting tests")
    with pytest.raises(ValueError, match="conflicted"):
        test_refresh.refresh(conn, project, identifier, source)
    assert repo.head_commit(work) == old and repo.is_clean(work)
    assert git(work, "rev-parse", f"archive/agentkit/test-{identifier}-{old[:12]}").stdout.strip() == old
    assert db.get_task(conn, identifier)["status"] == "BLOCKED"


def test_failed_gate_still_records_exact_preserved_refresh(conn, project_root, project, monkeypatch):
    from agentkit import gates
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    monkeypatch.setattr(gates, "run_gate", lambda *a, **kw: gates.GateResult("fast", False, skipped_reason="independent defect"))
    result = test_refresh.refresh(conn, project, identifier, source)
    assert not result["passed"] and db.get_task(conn, identifier)["status"] == "FAILED"
    assert git(work, "rev-parse", result["backup"]).stdout.strip() == old
    checkpoint = db.latest_checkpoint(conn, identifier, kind="mechanical")
    assert checkpoint["payload"]["head_sha"] == result["head"]
    assert repo.is_clean(work)
