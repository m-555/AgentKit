"""Report anticipated tool refusals to the agent; keep masking crashes.

MCP 2.x reports any exception that is not a ToolError as only
"Error executing tool <name>". AgentKit refuses work with ValueError,
PermissionError and RuntimeError, and an agent that cannot read the reason
retries blindly. Wrapping registration at the server covers every tool,
including ones added later, without each tool converting its own errors.
"""
from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from typing import Any

from .secrets import redact_text

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP 1.x
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

#: Refusals AgentKit raises on purpose. A TypeError, KeyError or similar is a
#: crash, and the SDK keeps its details from the caller.
REFUSALS = (ValueError, PermissionError, RuntimeError, FileNotFoundError, TimeoutError,
            sqlite3.OperationalError)
_LIMIT = 3000


def _surfaced(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except REFUSALS as error:
            raise ToolError(redact_text(str(error) or type(error).__name__)[-_LIMIT:]) from error
    call.__agentkit_refusals__ = True  # type: ignore[attr-defined]
    return call


def surface_refusals(server: Any) -> None:
    """Make every later registration, ``server.tool()`` or ``server.add_tool()``,
    report AgentKit's refusals.

    The decorated name keeps the original function, so direct Python callers
    (the CLI and tests) still receive the original exception type.
    """
    register, add = server.tool, server.add_tool

    def tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorate = register(*args, **kwargs)

        def apply(fn: Callable[..., Any]) -> Callable[..., Any]:
            decorate(_surfaced(fn))
            return fn
        return apply

    def add_tool(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return add(_surfaced(fn), *args, **kwargs)

    server.tool = tool
    server.add_tool = add_tool
