"""Prepare an untouched checkout without losing worker history or environments."""
import subprocess

import pytest
from conftest import git

from agentkit import (
    checkpoints,
    db,
    empty_refresh,
    host_completion,
    manager_state,
    recovery_store,
    repo,
    worktrees,
)


def setup(conn, project_root, project, monkeypatch):
    monkeypatch.setattr(host_completion, "authority", lambda *args: "manager")
    monkeypatch.setattr(manager_state, "pending", lambda *args: False)
    identifier = db.create_task(conn, title="held", spec_id="held", status="BLOCKED", job_id="job")
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    old = repo.head_commit(work)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=old, last_commit=old)
    integration = project_root.parent / "integration-source"
    git(project_root, "worktree", "add", str(integration), "integration")
    (integration / "services/retry.py").write_text("VALUE = 'accepted'\n")
    git(integration, "add", "services/retry.py")
    assert git(integration, "commit", "-qm", "accepted input").returncode == 0
    return identifier, work, old, repo.head_commit(integration)


def test_refresh_retains_checkout_and_records_new_base(conn, project_root, project, monkeypatch):
    identifier, work, old, new = setup(conn, project_root, project, monkeypatch)
    branch = repo.current_branch(work)
    marker = work / ".ai/runtime/prepared.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("private setup")
    assert repo.is_clean(work)
    result = empty_refresh.refresh(conn, project, identifier)
    task = db.get_task(conn, identifier)
    assert result["changed"] and task["status"] == "BLOCKED"
    assert task["base_sha"] == task["last_commit"] == repo.head_commit(work) == new
    assert repo.current_branch(work) == branch and marker.read_text() == "private setup"
    assert repo.is_ancestor(work, old, new)
    checkpoint = db.latest_checkpoint(conn, identifier, kind="mechanical")
    assert checkpoint["payload"]["head_sha"] == new
    assert empty_refresh.refresh(conn, project, identifier)["changed"] is False


@pytest.mark.parametrize("committed", [False, True])
def test_worker_edits_never_fast_forward(conn, project_root, project, monkeypatch, committed):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    path = work / "services/media.py"
    path.write_text("VALUE = 'preserved worker work'\n")
    if committed:
        git(work, "add", "services/media.py")
        git(work, "commit", "-qm", "worker work")
    head = repo.head_commit(work)
    with pytest.raises(ValueError):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == head and "preserved" in path.read_text()
    assert db.get_task(conn, identifier)["base_sha"] == old


def test_pending_quota_proof_blocks_refresh(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    recovery_store.register(conn, identifier="session", provider="codex", account="shared", host="cli", role="worker", reference="worker", task_id=identifier)
    recovery_store.arm(conn, "session", "authorization", {"head": old}, "quota")
    with pytest.raises(PermissionError, match="recovery proof"):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == old


def test_host_crash_after_git_is_recovered_from_journal(conn, project_root, project, monkeypatch):
    identifier, work, old, new = setup(conn, project_root, project, monkeypatch)
    update = db.update_task
    def crash(*args, **kwargs):
        raise RuntimeError("host stopped after git")
    monkeypatch.setattr(db, "update_task", crash)
    with pytest.raises(RuntimeError, match="host stopped"):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == new and db.get_task(conn, identifier)["base_sha"] == old
    monkeypatch.setattr(db, "update_task", update)
    result = empty_refresh.refresh(conn, project, identifier)
    assert result["head"] == new and db.get_task(conn, identifier)["base_sha"] == new
    assert repo.is_clean(work)


def test_worker_cannot_request_refresh(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    monkeypatch.setenv("AGENTKIT_TASK", str(identifier))
    with pytest.raises(PermissionError, match="host"):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == old


def test_diverged_integration_preserves_checkout(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    integration = project_root.parent / "integration-source"
    assert git(integration, "checkout", "--orphan", "unrelated").returncode == 0
    assert git(integration, "commit", "-qm", "unrelated history").returncode == 0
    subprocess.run(["git", "branch", "-f", "integration", "HEAD"], cwd=integration, check=True, capture_output=True)
    with pytest.raises(ValueError, match="descends"):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == old


def test_checkpoint_failure_after_database_update_is_recovered(conn, project_root, project, monkeypatch):
    identifier, work, old, new = setup(conn, project_root, project, monkeypatch)
    checkpoints.write_mechanical(conn, project_root, work, identifier)
    save = checkpoints.write_mechanical
    def crash(*args, **kwargs):
        raise RuntimeError("checkpoint stopped")
    monkeypatch.setattr(checkpoints, "write_mechanical", crash)
    with pytest.raises(RuntimeError, match="checkpoint stopped"):
        empty_refresh.refresh(conn, project, identifier)
    assert repo.head_commit(work) == db.get_task(conn, identifier)["base_sha"] == new
    assert db.latest_checkpoint(conn, identifier, kind="mechanical")["payload"]["head_sha"] == old
    monkeypatch.setattr(checkpoints, "write_mechanical", save)
    assert empty_refresh.refresh(conn, project, identifier)["head"] == new
    assert db.latest_checkpoint(conn, identifier, kind="mechanical")["payload"]["head_sha"] == new


def test_ready_preparation_failure_is_held_before_refresh(conn, project_root, project, monkeypatch):
    identifier, work, old, new = setup(conn, project_root, project, monkeypatch)
    db.set_status(conn, identifier, "READY", actor="human")
    db.update_task(conn, identifier, blocker="Host preparation blocked: missing accepted input")
    assert empty_refresh.refresh(conn, project, identifier)["changed"]
    task = db.get_task(conn, identifier)
    assert task["status"] == "BLOCKED"
    assert task["base_sha"] == task["last_commit"] == repo.head_commit(work) == new
    assert repo.is_ancestor(work, old, new)


def test_ordinary_ready_task_is_not_refreshed(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    db.set_status(conn, identifier, "READY", actor="human")
    with pytest.raises(ValueError, match="preparation failure"):
        empty_refresh.refresh(conn, project, identifier)
    assert db.get_task(conn, identifier)["status"] == "READY"
    assert repo.head_commit(work) == old


def test_ready_preparation_failure_preserves_dirty_work(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    db.set_status(conn, identifier, "READY", actor="human")
    db.update_task(conn, identifier, blocker="Host preparation blocked: missing input")
    (work / "services/media.py").write_text("VALUE = 'keep me'\n")
    with pytest.raises(ValueError, match="clean"):
        empty_refresh.refresh(conn, project, identifier)
    assert db.get_task(conn, identifier)["status"] == "READY"
    assert repo.head_commit(work) == old
    assert "keep me" in (work / "services/media.py").read_text()


def test_ready_refresh_requires_stopped_authorized_owner(conn, project_root, project, monkeypatch):
    identifier, work, old, _ = setup(conn, project_root, project, monkeypatch)
    db.set_status(conn, identifier, "READY", actor="human")
    db.update_task(conn, identifier, blocker="Host preparation blocked: missing input")
    def denied(*args):
        raise PermissionError("worker owner is live")
    monkeypatch.setattr(host_completion, "authority", denied)
    with pytest.raises(PermissionError, match="live"):
        empty_refresh.refresh(conn, project, identifier)
    assert db.get_task(conn, identifier)["status"] == "READY"
    assert repo.head_commit(work) == old
