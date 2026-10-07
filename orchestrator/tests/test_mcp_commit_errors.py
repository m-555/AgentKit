"""Real MCP tool errors explain refusals but hide unexpected crash details."""
import asyncio

import pytest

from agentkit import mcp_commit_errors, mcp_workspaces

try:
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.mcpserver.tools.base import Tool
except ImportError:
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.fastmcp.tools.base import Tool


def call():
    tool = Tool.from_function(mcp_commit_errors.task_commit, name="task_commit")
    return asyncio.run(tool.run({"message": "bounded edit"}, None))


def test_expected_refusal_reaches_worker_with_redacted_details(monkeypatch):
    def refuse(_message):
        raise ValueError("static gate failed; API_TOKEN=private-value-123456")
    monkeypatch.setattr(mcp_workspaces, "task_commit", refuse)
    with pytest.raises(ToolError) as caught:
        call()
    assert "static gate failed" in str(caught.value)
    assert "Do not repeat" in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_unknown_crash_details_remain_masked(monkeypatch):
    def crash(_message):
        raise RuntimeError("sensitive unexpected internal state")
    monkeypatch.setattr(mcp_workspaces, "task_commit", crash)
    with pytest.raises(ToolError) as caught:
        call()
    assert "sensitive" not in str(caught.value)


def test_success_keeps_existing_receipt(monkeypatch):
    monkeypatch.setattr(mcp_workspaces, "task_commit", lambda message: "Committed exact-head")
    assert call() == "Committed exact-head"
