"""Retire only integrated clean checkouts; branches, commits and history survive."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import db, processes, repo, workspace_registry
from .integrator import integration_branch
from .locking import exclusive


def archive(conn, project, task):
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Only host code can archive workspaces")
    with exclusive(project.root, "workspace-archive"):
        work = Path(task.get("worktree") or project.root).resolve()
        saved = workspace_registry.stored(project.root, task)
        if task["status"] != "DONE" or work == project.root.resolve() or not saved:
            raise ValueError("Only recorded completed worker checkouts may be archived")
        if not work.exists() and saved.get("phase") == "archived":
            return saved
        if processes.owning(conn):
            raise ValueError("Ownership must be stopped before archive")
        if Path(saved["path"]).resolve() != work or not any(
                Path(e["worktree"]).resolve() == work for e in repo.worktree_list(project.root)):
            raise ValueError("Checkout does not match its durable Git registration")
        head = task.get("last_commit")
        if not head or repo.head_commit(work) != head or not repo.is_clean(work):
            raise ValueError("Uncommitted or changed work must be preserved")
        if repo.current_branch(work) != task.get("branch"):
            raise ValueError("Recorded branch differs")
        if not repo.is_ancestor(project.root, head, integration_branch(project)):
            raise ValueError("Commit is not integrated")
        subprocess.run(["git", "worktree", "remove", str(work)], cwd=project.root,
                       capture_output=True, check=True, timeout=300)
        value = workspace_registry.record(project.root, task, phase="archived",
                                           archived_head=head, archived_at=db.utcnow())
        db.log_event(conn, task["id"], "workspace_archived", detail=value)
        return value
