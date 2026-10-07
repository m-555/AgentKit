"""MCP tools added beside `mcp_server`: fixed read-only source access.

Registered on the same server object when `mcp_server` is imported, so every
runtime that loads the AgentKit MCP server gets them. They exist for reviewers
and coordinators whose sandbox cannot run a shell; see `sourceview`.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import ToolAnnotations

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


def source_read(path: str, start_line: int = 1, max_lines: int = 400, where: str | None = None,
                task_id: int | None = None) -> str:
    """Read a bounded, numbered line range of one tracked file at the exact commit."""
    return _view(sourceview.read, where=where, task_id=task_id, path=path, start_line=start_line,
                 max_lines=max_lines)


def source_search(text: str, path_glob: str = "", where: str | None = None, task_id: int | None = None,
                  limit: int = 100) -> str:
    """Fixed-string search across tracked files at the exact commit (no regular expressions)."""
    return _view(sourceview.search, where=where, task_id=task_id, text=text, path_glob=path_glob, limit=limit)


def source_diff(task_id: int | None = None, path: str = "", offset: int = 0, limit: int = 60000) -> str:
    """The task's committed change against its recorded base, with the commit list."""
    return _view(sourceview.diff, where="task", task_id=task_id, path=path, offset=offset, limit=limit)


def register(server) -> None:
    """Register on the passed instance, including when launched with python -m."""
    for tool in (source_list, source_read, source_search, source_diff):
        server.add_tool(tool, annotations=READ_ONLY)
