"""Opt-in pruning of reproducible environments after committed task integration."""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

from . import db, processes, repo, workspace_registry

ENVIRONMENTS = ("venv", ".venv", "node_modules")


def prune(conn, project, task) -> list[str]:
    if project.raw.get("worktree_environment_cleanup") is not True or task["status"] != "DONE":
        return []
    work = Path(task.get("worktree") or project.root).absolute()
    root = project.root.resolve()
    if work.resolve() == root or not (root / ".ai/runtime" / f"task-{task['id']}-setup").is_file():
        raise ValueError("Only provisioned worker environments may be pruned")
    record = workspace_registry.stored(root, task)
    if not record or Path(record.get("path", "")).resolve() != work.resolve():
        raise ValueError("Worker path is not in the durable registry")
    checkouts = repo.worktree_list(root)
    if not any(Path(entry.get("worktree", "")).resolve() == work.resolve()
               and entry.get("branch") == f"refs/heads/{task['branch']}" for entry in checkouts):
        raise ValueError("Worker is not registered to this repository and branch")
    if repo.head_commit(work) != task.get("last_commit") or not repo.is_clean(work):
        raise ValueError("Completed source has changed; preserve its environment")
    if any(p["task_id"] == task["id"] for p in processes.owning(conn)):
        raise ValueError("Worker ownership is not stopped")
    tracked = repo.tracked_files(work)
    targets = []
    for name in ENVIRONMENTS:
        target = work / name
        if not target.exists():
            continue
        if any(path == name or path.startswith(name + "/") for path in tracked):
            raise ValueError(f"{name} contains tracked source; preserve it")
        attributes = target.lstat()
        if (not target.is_dir() or target.is_symlink()
                or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or target.resolve().parent != work.resolve()):
            raise ValueError("Environment root is linked or outside the recorded worker")
        targets.append((name, target))
    removed = []
    for name, target in targets:
        # Python 3.8+ removes Windows junctions below this tree without traversing
        # their targets. Keep workspace package sources, the worktree and Git intact.
        shutil.rmtree(target)
        removed.append(name)
    if removed:
        db.log_event(conn, task["id"], "worker_environments_pruned",
                     detail={"worktree": str(work), "removed": removed, "source_head": task["last_commit"]})
    return removed


def after_merge(conn, project, task_id):
    try:
        task = db.get_task(conn, task_id)
        if project.raw.get("worktree_archive_completed") is True:
            from .workspace_archive import archive
            archive(conn, project, task)
            return ["checkout"]
        return prune(conn, project, task)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        db.log_event(conn, task_id, "worker_environment_cleanup_deferred", cause=str(exc))
        return []  # Cleanup failure never revokes passed integration or source.
