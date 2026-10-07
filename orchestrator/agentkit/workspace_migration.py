"""Journaled Git moves: stopped owners, unchanged content, recoverable registry updates."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import db, processes, repo, workspace_registry
from .locking import atomic_write, exclusive
from .worktree_storage import directory


def _operator():
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Workspace migration is a host/operator operation")


def _state(work):
    """Observe HEAD, index/worktree diff and untracked content without changing any."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=work, capture_output=True,
                              check=True, timeout=60).stdout
    digest = hashlib.sha256()
    for args in (("status", "--porcelain=v1", "-z"), ("diff", "--binary"),
                 ("diff", "--cached", "--binary")):
        digest.update(git(*args))
    for item in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not item:
            continue
        path = work / os.fsdecode(item)
        if path.is_symlink():
            content = os.readlink(path).encode()
        elif path.is_file():
            content = path.read_bytes()
        else:
            raise ValueError("Cannot verify untracked special file during move")
        digest.update(item)
        digest.update(content)
    return {"head": repo.head_commit(work), "state": digest.hexdigest(),
            "branch": repo.current_branch(work)}


def _complete(conn, project, journal):
    source, target = Path(journal["source"]), Path(journal["target"])
    actual = {Path(e["worktree"]).resolve(): e for e in repo.worktree_list(project.root)}
    if source.exists() or target.resolve() not in actual:
        raise ValueError("Ambiguous migration paths; preserve journal for recovery")
    if _state(target) != journal["evidence"]:
        raise ValueError("Moved workspace differs; preserve files and migration journal")
    if processes.owning(conn):
        raise ValueError("Worker ownership appeared during migration")
    with exclusive(project.root, "workspace-inventory"):
        data = workspace_registry._load(project.root)
        record = data["workspaces"].get(journal["spec_id"])
        if record:
            old = Path(record["path"]).resolve()
            if old not in (source.resolve(), target.resolve()):
                raise ValueError("Registry changed concurrently")
            record.update(path=str(target), previous_path=str(source), updated_at=db.utcnow())
            atomic_write(workspace_registry.manifest(project.root), json.dumps(data, indent=2) + "\n")
    with db.immediate_transaction(conn):
        for task in db.list_tasks(conn):
            if task.get("worktree") and Path(task["worktree"]).resolve() == source.resolve():
                db.update_task(conn, task["id"], worktree=str(target))
        task_id = journal.get("task_id")
        assigned = db.get_task(conn, task_id) if task_id else None
        if assigned and not assigned.get("worktree"):
            db.update_task(conn, task_id, worktree=str(target), branch=journal["evidence"]["branch"])
        db.log_event(conn, journal.get("task_id"), "workspace_moved",
                     detail={**journal, "phase": "committed"})
    journal["phase"] = "committed"


def move(conn, project, task, target, *, preserve_dirty=False):
    _operator()
    target = Path(target).absolute()
    source = Path(task["worktree"]).resolve(strict=True)
    allowed = directory(project.root, project)
    if target.parent.resolve() != allowed.resolve() or target.resolve() == source:
        raise ValueError("Destination must be a direct child of configured worktree_root")
    if source == project.root.resolve() or processes.owning(conn):
        raise ValueError("Operator checkout or live ownership cannot be moved")
    if not repo.is_clean(source) and not preserve_dirty:
        raise ValueError("Dirty workspace requires explicit preserve_dirty migration")
    entries = repo.worktree_list(project.root)
    if not any(Path(e["worktree"]).resolve() == source for e in entries):
        raise ValueError("Source is not this repository's registered worktree")
    if task.get("branch") and repo.current_branch(source) != task["branch"]:
        raise ValueError("Source branch differs from task")
    if target.exists():
        raise ValueError("Destination already exists")
    evidence = _state(source)
    key = hashlib.sha256(str(source).encode()).hexdigest()[:20]
    path = project.root / ".ai/runtime/workspace-migrations/journals" / f"{key}.json"
    with exclusive(project.root, "supervisor"), exclusive(project.root, "integration"):
        if processes.owning(conn):
            raise ValueError("Worker ownership must be stopped")
        if _state(source) != evidence:
            raise ValueError("Workspace changed before migration")
        if path.exists() and json.loads(path.read_text())["phase"] != "committed":
            raise ValueError("Recover the previous migration first")
        target.parent.mkdir(parents=True, exist_ok=True)
        journal = {"phase": "prepared", "source": str(source), "target": str(target),
                   "spec_id": workspace_registry.key(task), "task_id": task.get("id"),
                   "evidence": evidence, "preserve_dirty": preserve_dirty}
        atomic_write(path, json.dumps(journal, indent=2) + "\n")
        subprocess.run(["git", "worktree", "move", str(source), str(target)],
                       cwd=project.root, check=True, capture_output=True, timeout=300)
        journal["phase"] = "git_moved"
        atomic_write(path, json.dumps(journal, indent=2) + "\n")
        _complete(conn, project, journal)
        atomic_write(path, json.dumps(journal, indent=2) + "\n")
    return journal


def recover(conn, project):
    _operator()
    result = []
    with exclusive(project.root, "supervisor"), exclusive(project.root, "integration"):
        for path in sorted((project.root / ".ai/runtime/workspace-migrations/journals").glob("*.json")):
            journal = json.loads(path.read_text())
            if journal["phase"] == "committed":
                continue
            source, target = Path(journal["source"]), Path(journal["target"])
            if source.exists() and not target.exists() and journal["phase"] == "prepared":
                if _state(source) != journal["evidence"]:
                    raise ValueError("Prepared migration source changed")
                journal["phase"] = "cancelled_before_move"
            elif journal["phase"] == "cancelled_before_move":
                continue
            else:
                _complete(conn, project, journal)
            atomic_write(path, json.dumps(journal, indent=2) + "\n")
            result.append(journal)
    return result


def pending(root):
    return any(json.loads(p.read_text())["phase"] not in ("committed", "cancelled_before_move")
               for p in (Path(root) / ".ai/runtime/workspace-migrations/journals").glob("*.json"))
