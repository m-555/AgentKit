"""Real Git moves preserve staged, unstaged, untracked work and recover journals."""
from __future__ import annotations

import json

import pytest
from conftest import git

from agentkit import db, repo, workspace_registry, worktrees
from agentkit import workspace_migration as migration
from agentkit.worktree_storage import directory


def workspace(conn, project):
    task = db.get_task(conn, db.create_task(conn, title="move", spec_id="move"))
    work, _ = worktrees.ensure(project.root, task, project)
    db.update_task(conn, task["id"], worktree=str(work), branch=repo.current_branch(work))
    return db.get_task(conn, task["id"]), work


def test_default_grouped_directory_and_explicit_external_root(project):
    assert directory(project.root).name == "wt-demo"
    project.raw["worktree_root"] = str(project.root.parent / "wt-other")
    assert directory(project.root, project).name == "wt-other"
    for invalid in (str(project.root), str(project.root / "nested"), str(project.root.parent), "relative"):
        project.raw["worktree_root"] = invalid
        with pytest.raises(ValueError):
            directory(project.root, project)


def test_move_preserves_commit_and_records(conn, project):
    task, work = workspace(conn, project)
    project.raw["worktree_root"] = str(project.root.parent / "group")
    target = directory(project.root, project) / work.name
    head = repo.head_commit(work)
    migration.move(conn, project, task, target)
    assert not work.exists() and repo.head_commit(target) == head
    assert db.get_task(conn, task["id"])["worktree"] == str(target)
    assert workspace_registry.stored(project.root, task)["path"] == str(target)
    assert not migration.pending(project.root)
    assert git(target, "rev-parse", "--git-common-dir").returncode == 0


def test_dirty_requires_explicit_preservation_and_keeps_every_source_change(conn, project):
    task, work = workspace(conn, project)
    (work / "services/media.py").write_text("staged")
    git(work, "add", "services/media.py")
    (work / "services/retry.py").write_text("unstaged")
    (work / "draft.txt").write_text("untracked")
    target = directory(project.root, project) / "moved"
    before = migration._state(work)
    with pytest.raises(ValueError, match="Dirty"):
        migration.move(conn, project, task, target)
    migration.move(conn, project, task, target, preserve_dirty=True)
    assert migration._state(target) == before
    assert (target / "draft.txt").read_text() == "untracked"


def test_resume_after_git_move_updates_registry_without_second_move(conn, project, monkeypatch):
    task, work = workspace(conn, project)
    target = directory(project.root, project) / "recover"
    complete = migration._complete
    monkeypatch.setattr(migration, "_complete", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        migration.move(conn, project, task, target)
    assert migration.pending(project.root) and target.exists()
    monkeypatch.setattr(migration, "_complete", complete)
    migration.recover(conn, project)
    assert db.get_task(conn, task["id"])["worktree"] == str(target)
    assert not migration.pending(project.root)


def test_refuses_changed_recovery_destination_and_worker_calls(conn, project, monkeypatch):
    task, work = workspace(conn, project)
    target = directory(project.root, project) / "blocked"
    monkeypatch.setenv("AGENTKIT_TASK", "1")
    with pytest.raises(PermissionError):
        migration.move(conn, project, task, target)
    monkeypatch.delenv("AGENTKIT_TASK")
    with pytest.raises(ValueError, match="direct child"):
        migration.move(conn, project, task, project.root / "unsafe")


def test_pending_journal_blocks_launch_inventory(project):
    folder = project.root / ".ai/runtime/workspace-migrations/journals"
    folder.mkdir(parents=True)
    (folder / "move.json").write_text(json.dumps({"phase": "git_moved"}))
    assert migration.pending(project.root)


def test_existing_foreign_checkout_is_not_adopted(conn, project):
    task = db.get_task(conn, db.create_task(conn, title="foreign", spec_id="foreign"))
    target = worktrees.path_for(project.root, task, project)
    target.mkdir(parents=True)
    git(target, "init", "-q", "-b", worktrees.branch_name(task))
    with pytest.raises(ValueError, match="another Git repository"):
        worktrees.ensure(project.root, task, project)
