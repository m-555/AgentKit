"""Host-only audited commits and workspace inventory for every provider."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import audit, db, gates, repo, worker, workspace_registry
from .config import load_project
from .git_staging import stage
from .locking import exclusive
from .mcp_extra import READ_ONLY


def task_commit(message: str) -> str:
    """Commit audited task files on the host. Message: 1-2000 chars. Applies optional source_line_endings."""
    if not isinstance(message, str) or not message.strip() or len(message) > 2000 or "\0" in message:
        raise ValueError("Commit message must contain 1-2000 characters")
    root = Path(os.environ["AGENTKIT_ROOT"])
    task_id = int(os.environ["AGENTKIT_TASK"])
    if os.environ.get("AGENTKIT_ROLE", "worker") != "worker":
        raise PermissionError("Only the assigned worker can commit task changes")
    with exclusive(root, f"commit-task-{task_id}"):
        conn = db.connect(root)
        try:
            task = worker.require_current(conn, task_id).task
            if task["status"] != "RUNNING" or not task.get("worktree") or not task.get("branch"):
                raise ValueError("A running worker branch is required")
            work = Path(task["worktree"]).resolve(strict=True)
            if repo.current_branch(work) != task["branch"]:
                raise ValueError("Assigned branch differs from current worktree")
            project = load_project(root)
            result = audit.audit_worktree(conn, project, work, task_id)
            if not result.clean:
                raise PermissionError(result.summary())
            changed = repo.changed_files(work)
            if not changed:
                return "Nothing to commit: " + repo.head_commit(work)
            from .commit_format import normalize
            head_before = repo.head_commit(work)
            normalize(project, work, changed, lambda: worker.require_current(conn, task_id))
            before = gates.run_gate(project, "fast", cwd=work)
            if not before.passed:
                raise ValueError(before.summary())
            current = worker.require_current(conn, task_id).task
            if (current["status"] != "RUNNING" or current.get("branch") != task["branch"]
                    or current.get("worktree") != task["worktree"]
                    or repo.current_branch(work) != task["branch"]
                    or repo.head_commit(work) != head_before):
                raise PermissionError("Task branch or authority changed during host checks")
            result = audit.audit_worktree(conn, project, work, task_id)
            if not result.clean:
                raise PermissionError(result.summary())
            stage(work, repo.changed_files(work))
            staged = audit.audit_staged(conn, project, work, task_id)
            if not staged.clean:
                raise PermissionError(staged.summary())
            worker.require_current(conn, task_id)
            subprocess.run(["git", "commit", "-m", message], cwd=work, capture_output=True, check=True, timeout=120)
            head = repo.head_commit(work)
            db.update_task(conn, task_id, last_commit=head)
            workspace_registry.record(root, task, path=str(work), branch=task["branch"], phase="committed", head=head)
            return "Committed " + head + "; task and full integration gates still required"
        finally:
            conn.close()


def workspaces() -> str:
    """Discover recorded and orphaned branches/worktrees without creating or deleting any."""
    root = Path(os.environ["AGENTKIT_ROOT"])
    conn = db.connect_readonly(root)
    try:
        return json.dumps(workspace_registry.inventory(root, db.list_tasks(conn)), indent=2)
    finally:
        conn.close()



def test_hold(task_id: int, source_task_id: int, evidence: str) -> str:
    """Hold exact stopped tests for host refresh; never launches another model."""
    from .test_hold import hold
    root = Path(os.environ["AGENTKIT_ROOT"])
    conn = db.connect(root)
    try:
        return json.dumps(hold(conn, load_project(root), task_id, source_task_id, evidence))
    finally:
        conn.close()


def test_refresh(task_id: int, source_task_id: int) -> str:
    """Host carries unchanged held tester commits onto an accepted source correction."""
    from .test_refresh import refresh
    root = Path(os.environ["AGENTKIT_ROOT"])
    conn = db.connect(root)
    try:
        return json.dumps(refresh(conn, load_project(root), task_id, source_task_id))
    finally:
        conn.close()


def task_refresh_empty(task_id: int) -> str:
    """Host refreshes a stopped, untouched checkout without creating an AI session."""
    from .empty_refresh import refresh
    root = Path(os.environ["AGENTKIT_ROOT"])
    conn = db.connect(root)
    try:
        return json.dumps(refresh(conn, load_project(root), task_id))
    finally:
        conn.close()



def source_refresh(task_id: int, source_task_id: int) -> str:
    """Host prepares accepted dependencies for a held exact source draft; it stays held."""
    from .test_refresh import refresh_source
    root = Path(os.environ["AGENTKIT_ROOT"])
    conn = db.connect(root)
    try:
        return json.dumps(refresh_source(conn, load_project(root), task_id, source_task_id))
    finally:
        conn.close()


def register(server):
    from .mcp_commit_errors import task_commit as checked_commit
    server.add_tool(checked_commit, name="task_commit")
    server.add_tool(test_hold)
    server.add_tool(test_refresh)
    server.add_tool(source_refresh)
    server.add_tool(task_refresh_empty)
    server.add_tool(workspaces, annotations=READ_ONLY)
