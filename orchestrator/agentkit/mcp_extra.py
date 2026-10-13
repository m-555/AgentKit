"""MCP tools added beside `mcp_server`: fixed read-only source access.

Registered on the same server object when `mcp_server` is imported, so every
runtime that loads the AgentKit MCP server gets them. They exist for reviewers
and coordinators whose sandbox cannot run a shell; see `sourceview`.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import ToolAnnotations

try:
    from mcp.server.mcpserver import Image
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import Image  # type: ignore[attr-defined,no-redef]

from . import db, sourceview
from .config import load_project
from .paths import find_project_root

READ_ONLY = ToolAnnotations.model_validate({"readOnlyHint": True, "destructiveHint": False,
                                          "idempotentHint": True, "openWorldHint": False})


def _root():
    import os
    root = find_project_root(os.environ.get("AGENTKIT_ROOT") or os.getcwd())
    if root is None:
        raise RuntimeError("No project found; run agentkit init or set AGENTKIT_ROOT")
    return root


def _dump(value):
    return json.dumps(value, indent=2, default=str)


def _view(call, *, where: str | None, task_id: int | None, **kwargs: Any) -> str:
    root = _root()
    conn = db.connect_readonly(root)
    try:
        view = sourceview.scope(conn, load_project(root), where=where, task_id=task_id)
        result = call(view, **kwargs)
        return _dump(result)
    finally:
        conn.close()


def source_list(prefix: str = "", pattern: str = "", where: str | None = None, task_id: int | None = None,
                offset: int = 0, limit: int = 500) -> str:
    """List tracked files at the exact commit you may read (read-only; no shell needed).

    Reviewers see their assigned commit. Coordinators choose where=task (with
    task_id), integration or project. Credential-shaped files are withheld.
    """
    return _view(sourceview.listing, where=where, task_id=task_id, prefix=prefix, pattern=pattern,
                 offset=offset, limit=limit)


MAX_READ_FILES = 12


def source_read(path: str = "", start_line: int = 1, max_lines: int = 400, where: str | None = None,
                task_id: int | None = None, paths: list[str] | None = None) -> str:
    """Read a bounded, numbered line range of a tracked file at the exact commit.

    Pass paths (up to 12) instead of path to read several files in one call.
    """
    if paths:
        if path or len(paths) > MAX_READ_FILES:
            raise ValueError(f"pass path or up to {MAX_READ_FILES} paths, not both")
        return _view(_read_many, where=where, task_id=task_id, paths=list(paths), start_line=start_line,
                     max_lines=max_lines)
    if not path:
        raise ValueError("pass path or paths")
    return _view(sourceview.read, where=where, task_id=task_id, path=path, start_line=start_line,
                 max_lines=max_lines)


def _read_many(view, *, paths: list[str], start_line: int, max_lines: int) -> list[dict]:
    return [sourceview.read(view, path, start_line=start_line, max_lines=max_lines) for path in paths]


def source_search(text: str, path_glob: str = "", where: str | None = None, task_id: int | None = None,
                  limit: int = 100) -> str:
    """Fixed-string search across tracked files at the exact commit (no regular expressions)."""
    return _view(sourceview.search, where=where, task_id=task_id, text=text, path_glob=path_glob, limit=limit)


def source_diff(task_id: int | None = None, path: str = "", offset: int = 0, limit: int = 60000) -> str:
    """The task's committed change against its recorded base, with the commit list."""
    return _view(sourceview.diff, where="task", task_id=task_id, path=path, offset=offset, limit=limit)


def job_attachment(job_id: str, name: str) -> Image:
    """Open one screenshot the user attached to a job; the job's requests name them (r1-1.png ...)."""
    from . import job_input
    data, kind = job_input.read(_root(), job_id, name)
    return Image(data=data, format=kind)


def job_review_submit(job_id: str, head_sha: str, verdict: str, evidence: str) -> str:
    """Your one review of a whole job, at the combined commit you were given.

    verdict: PASS, CHANGES or REJECT for the code; BLOCKED when a tool, permission or setup
    failure kept you from reviewing (the user is asked; nobody redoes the work)."""
    from . import db, job_acceptance, processes
    from .config import load_project
    root = _root()
    conn = db.connect(root)
    try:
        process = processes.require_control(conn, purposes=("final_review",), job_id=job_id)
        if head_sha != process["expected_head"]:
            raise ValueError("reviewed commit changed")
        job_acceptance.record(conn, load_project(root), job_id, head_sha, verdict,
                              f"{process['provider']}:process-{process['id']}", evidence)
        return "Final review recorded."
    finally:
        conn.close()


def register(server) -> None:
    """Register on the passed instance, including when launched with python -m."""
    for tool in (source_list, source_read, source_search, source_diff, job_attachment):
        server.add_tool(tool, annotations=READ_ONLY)
    server.add_tool(job_review_submit)
