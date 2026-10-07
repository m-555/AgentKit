"""Durable inventory of task branches and worktrees; discovery never deletes work."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import db, repo
from .locking import atomic_write, exclusive


def manifest(root: str | Path) -> Path:
    return Path(root) / ".ai" / "runtime" / "workspaces.json"


def _load(root: str | Path) -> dict:
    path = manifest(root)
    if not path.exists():
        return {"version": 1, "workspaces": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("workspaces"), dict):
        raise ValueError("Invalid workspace inventory; preserve it for recovery")
    return data


def key(task: dict[str, Any]) -> str:
    # Stable across retries and database rebuilds, unlike the runtime task number.
    return str(task.get("spec_id") or f"task-{task['id']}")


def stored(root: str | Path, task: dict[str, Any]) -> dict | None:
    return _load(root)["workspaces"].get(key(task))


def record(root: str | Path, task: dict[str, Any], **changes: Any) -> dict:
    with exclusive(root, "workspace-inventory"):
        data = _load(root)
        previous = data["workspaces"].get(key(task), {})
        for field in ("path", "branch"):
            if previous.get(field) and field in changes and previous[field] != changes[field]:
                raise ValueError(f"Workspace {key(task)} cannot silently change {field}")
        value = {**previous, "spec_id": key(task), "task_id": task["id"],
                 "job_id": task.get("job_id"), "updated_at": db.utcnow(), **changes}
        data["workspaces"][key(task)] = value
        path = manifest(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(data, indent=2) + "\n")
        return value


def suffix(task: dict[str, Any]) -> str:
    return hashlib.sha256(key(task).encode()).hexdigest()[:10]


def inventory(root: str | Path, tasks: list[dict[str, Any]]) -> list[dict]:
    """Join durable records, task rows and Git, including unknown agent branches."""
    records = {name: dict(value) for name, value in _load(root)["workspaces"].items()}
    for task in tasks:
        if not task.get("branch") or not task.get("worktree"):
            continue
        name = key(task)
        value = records.setdefault(name, {"spec_id": name})
        value.update(task_id=task["id"], job_id=task.get("job_id"), status=task["status"])
        value.setdefault("branch", task["branch"])
        value.setdefault("path", str(task["worktree"]))
    known = {value.get("branch") for value in records.values()}
    # Git inventory is independent of SQLite and survives a lost tasks.db.
    branches = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/agent/"], cwd=root, capture_output=True, text=True, check=True, timeout=30)
    for branch in branches.stdout.splitlines():
        if branch not in known:
            records[branch] = {"spec_id": None, "branch": branch, "phase": "unregistered", "attention": "Unregistered branch; recover ownership before use"}
    checkouts = {entry.get("branch", "").removeprefix("refs/heads/"): entry.get("worktree")
                 for entry in repo.worktree_list(root)}
    result = []
    for value in records.values():
        branch = value.get("branch", "")
        actual = checkouts.get(branch)
        value["checkout"] = str(Path(actual)) if actual else None
        value["branch_exists"] = repo.branch_exists(root, branch) if branch else False
        value["head"] = repo.head_commit(actual) if actual and Path(actual).is_dir() else ""
        if actual and value.get("path") and Path(actual).resolve() != Path(value["path"]).resolve():
            value["attention"] = "Branch is checked out at a different path; never create a competing checkout"
        elif value.get("path") and Path(value["path"]).exists() and actual is None:
            value["attention"] = "Reserved checkout is on another branch or unregistered; inspect before adoption"
        elif value.get("path") and not Path(value["path"]).exists() and value.get("phase") != "archived":
            value["attention"] = "Worktree missing; branch and inventory retained for recovery"
        result.append(value)
    return result


def track_merge(project, task, merge, target):
    reservation = {field: value for field, value in
                   (("path", task.get("worktree")), ("branch", task.get("branch"))) if value}
    record(project.root, task, **reservation, phase="integration-pending")
    outcome = merge()
    record(project.root, task, phase="merged" if outcome.ok else "held",
           integration_branch=target(), detail=outcome.detail[:500],
           integration_head=repo.rev_parse(project.root, target()) if outcome.ok else None)
    return outcome
