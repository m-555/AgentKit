"""Expose anticipated host commit refusals, retaining SDK masking of crashes."""
import subprocess

from .secrets import redact_text
from .worker import StaleGeneration

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP1.x
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]


def task_commit(message: str) -> str:
    """Commit assigned files through host guards; stop and report any refusal."""
    from .mcp_workspaces import task_commit as commit
    try:
        return commit(message)
    except (ValueError, PermissionError, StaleGeneration, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr or "Git rejected the commit; inspect the host diagnostic"
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
        else:
            detail = str(error)
        raise ToolError("Host commit blocked: " + redact_text(detail)[-3000:]
                        + "\nStop; report this reason to the manager. Do not repeat the commit call.") from None
