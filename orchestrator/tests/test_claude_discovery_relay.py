"""Discovery and Unicode relay must work before source workers consume turns."""
import io
import json

from agentkit import wsl_host
from agentkit.adapters.claude_code import ClaudeCodeAdapter


def test_worker_keeps_mcp_discovery_available(project, tmp_path, monkeypatch):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    adapter = ClaudeCodeAdapter()
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"kind": "SAFE_PARALLEL"}, tmp_path, "backend-builder", project, prompt="Brief only")
    tools = launch.argv[launch.argv.index("--tools") + 1].split(",")
    assert {"ToolSearch", "WaitForMcpServers", "Read", "Write"} <= set(tools)


def test_wsl_json_output_survives_a_non_utf8_windows_pipe(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(wsl_host.sys, "stdout", stream)
    message = json.dumps({"text": "\u4e2d\u6587 \u2264 \u2192"}, ensure_ascii=False) + "\n"
    wsl_host.write_native(message)
    assert buffer.getvalue() == message.encode("utf-8")
    assert json.loads(buffer.getvalue())["text"] == "\u4e2d\u6587 \u2264 \u2192"
