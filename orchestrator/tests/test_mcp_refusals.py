"""F20: AgentKit's refusals reach the agent; crashes stay masked.

MCP 2.x reports any exception other than ToolError as only "Error executing
tool <name>". AgentKit refuses work with ValueError, PermissionError and
RuntimeError, so after the SDK upgrade a manager whose task definition was over
budget, or a worker refused a lease, saw no reason at all and retried blindly.
The server wraps every registration, so a new tool is covered without
remembering to convert its own errors.
"""
import asyncio

import pytest

from agentkit import mcp_server
from agentkit.mcp_errors import ToolError, surface_refusals

TOKEN = "ghp_" + "A" * 36


def _call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))


def _server():
    server = mcp_server._Server("refusal-probe")
    surface_refusals(server)
    return server


def test_every_registered_tool_reports_its_refusals():
    tools = mcp_server.mcp._tool_manager._tools
    assert len(tools) > 30
    assert [name for name, tool in tools.items() if not getattr(tool.fn, "__agentkit_refusals__", False)] == []


def test_a_refusal_reaches_the_caller_with_its_reason(project_root, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    with pytest.raises(ToolError) as caught:
        _call(mcp_server.mcp, "brief", {})
    assert "No active task" in str(caught.value)


@pytest.mark.parametrize("error", [ValueError, PermissionError, RuntimeError, FileNotFoundError])
def test_each_refusal_type_keeps_its_message(error):
    server = _server()

    @server.tool()
    def refuse() -> str:
        raise error("over max_task_chars")

    with pytest.raises(ToolError) as caught:
        _call(server, "refuse", {})
    assert "over max_task_chars" in str(caught.value)


def test_a_refusal_never_discloses_a_credential():
    server = _server()

    @server.tool()
    def leaky() -> str:
        raise ValueError(f"push rejected for {TOKEN}")

    with pytest.raises(ToolError) as caught:
        _call(server, "leaky", {})
    assert "push rejected" in str(caught.value) and TOKEN not in str(caught.value)


def test_a_crash_is_still_masked():
    server = _server()

    @server.tool()
    def broken() -> str:
        raise TypeError("internal detail")

    with pytest.raises(ToolError) as caught:
        _call(server, "broken", {})
    assert "internal detail" not in str(caught.value)


def test_a_direct_python_caller_still_gets_the_original_exception(project_root, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    with pytest.raises(RuntimeError, match="No active task"):
        mcp_server.brief()
