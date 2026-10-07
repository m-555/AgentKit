"""Environment cleanup preserves committed source and refuses uncertain ownership."""
import os
import subprocess
from pathlib import Path

import pytest

from agentkit import db, repo, workspace_registry, worktree_environments, worktrees
from agentkit.config import load_project


def prepared(project_root, monkeypatch):
    project = load_project(project_root)
    project.raw["worktree_environment_cleanup"] = True
    conn = db.connect(project_root)
    identifier = db.create_task(conn, title="finished", kind="SAFE_PARALLEL", owned_paths=["services/media.py"])
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    db.update_task(conn, identifier, status="DONE", worktree=str(work), branch=repo.current_branch(work),
                   last_commit=repo.head_commit(work))
    marker = project_root / ".ai/runtime" / f"task-{identifier}-setup"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("provisioned")
    for name in worktree_environments.ENVIRONMENTS:
        (work / name).mkdir()
        (work / name / "installed.bin").write_bytes(b"reproducible")
    exclude = Path(repo._git(["rev-parse", "--git-path", "info/exclude"], work).strip())
    if not exclude.is_absolute():
        exclude = work / exclude
    with exclude.open("a") as handle:
        handle.write("\n/venv/\n/.venv/\n/node_modules/\n")
    monkeypatch.setattr(worktree_environments.processes, "owning", lambda conn: [])
    return conn, project, db.get_task(conn, identifier), work


def test_prunes_only_envs_and_retains_registry_branches_source(project_root, monkeypatch):
    conn, project, task, work = prepared(project_root, monkeypatch)
    head = repo.head_commit(work)
    assert worktree_environments.prune(conn, project, task) == list(worktree_environments.ENVIRONMENTS)
    assert repo.head_commit(work) == head
    assert (work / "services/media.py").is_file()
    assert workspace_registry.stored(project_root, task)["path"] == str(work)
    assert repo.branch_exists(project_root, task["branch"])
    assert worktree_environments.prune(conn, project, task) == []


@pytest.mark.parametrize("reason", ["disabled", "unfinished", "dirty", "running", "no_marker", "wrong_registry"])
def test_preserves_uncertain_or_unapproved_environment(project_root, monkeypatch, reason):
    conn, project, task, work = prepared(project_root, monkeypatch)
    if reason == "disabled":
        project.raw["worktree_environment_cleanup"] = False
    elif reason == "unfinished":
        task["status"] = "REVIEW"
    elif reason == "dirty":
        (work / "services/media.py").write_text("changed")
    elif reason == "running":
        monkeypatch.setattr(worktree_environments.processes, "owning", lambda conn: [{"task_id": task["id"]}])
    elif reason == "no_marker":
        (project_root / ".ai/runtime" / f"task-{task['id']}-setup").unlink()
    else:
        monkeypatch.setattr(worktree_environments.workspace_registry, "stored", lambda root, task: {"path": str(project_root)})
    if reason in ("disabled", "unfinished"):
        assert worktree_environments.prune(conn, project, task) == []
    else:
        with pytest.raises(ValueError):
            worktree_environments.prune(conn, project, task)
    assert all((work / name / "installed.bin").exists() for name in worktree_environments.ENVIRONMENTS)


def test_tracked_environment_is_never_deleted(project_root, monkeypatch):
    conn, project, task, work = prepared(project_root, monkeypatch)
    subprocess.run(["git", "add", "-f", "node_modules/installed.bin"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked dependency source"], cwd=work, check=True, capture_output=True)
    task["last_commit"] = repo.head_commit(work)
    with pytest.raises(ValueError, match="tracked source"):
        worktree_environments.prune(conn, project, task)
    assert all((work / name / "installed.bin").exists() for name in worktree_environments.ENVIRONMENTS)


def test_child_link_target_remains_untouched(project_root, monkeypatch):
    conn, project, task, work = prepared(project_root, monkeypatch)
    source = project_root / "services"
    try:
        os.symlink(source, work / "node_modules" / "workspace-package", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Symlink creation unavailable")
        link = str(work / "node_modules" / "workspace-package").replace("'", "''")
        destination = str(source).replace("'", "''")
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{link}' -Target '{destination}' | Out-Null"],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    before = (source / "media.py").read_bytes()
    worktree_environments.prune(conn, project, task)
    assert (source / "media.py").read_bytes() == before
