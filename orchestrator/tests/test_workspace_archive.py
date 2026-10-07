"""Retirement preserves committed history and refuses unfinished source."""
import pytest
from conftest import git

from agentkit import db, repo, workspace_registry, worktrees
from agentkit.integrator import integration_branch
from agentkit.workspace_archive import archive


def completed(conn, project):
    task = db.get_task(conn, db.create_task(conn, title="archive", spec_id="archive"))
    work, _ = worktrees.ensure(project.root, task, project)
    db.update_task(conn, task["id"], worktree=str(work), branch=repo.current_branch(work),
                   status="DONE", last_commit=repo.head_commit(work))
    return db.get_task(conn, task["id"]), work


def test_archive_preserves_branch_commit_and_inventory_history(conn, project):
    task, work = completed(conn, project)
    archive(conn, project, task)
    assert not work.exists()
    assert repo.branch_exists(project.root, task["branch"])
    assert repo.is_ancestor(project.root, task["last_commit"], integration_branch(project))
    assert workspace_registry.stored(project.root, task)["phase"] == "archived"
    assert archive(conn, project, task)["phase"] == "archived"


def test_archive_refuses_dirty_and_unintegrated_work(conn, project):
    task, work = completed(conn, project)
    (work / "draft.txt").write_text("preserve")
    with pytest.raises(ValueError, match="preserved"):
        archive(conn, project, task)
    assert work.exists()
    git(work, "add", "draft.txt")
    git(work, "commit", "-qm", "unintegrated")
    db.update_task(conn, task["id"], last_commit=repo.head_commit(work))
    with pytest.raises(ValueError, match="not integrated"):
        archive(conn, project, db.get_task(conn, task["id"]))
    assert (work / "draft.txt").read_text() == "preserve"
