"""Guard probes must observe tool enforcement, rather than model compliance."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit import probe
from agentkit.adapters.base import Installation
from agentkit.capabilities import CapabilitySet


@pytest.mark.parametrize("response", [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I will respect the lease and not attempt guarded.txt."}]}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "[AgentKit] Edit blocked: guarded.txt outside lease"}},
])
def test_refusal_cannot_certify_a_guard(monkeypatch, response):
    adapter = SimpleNamespace(
        supports_write_probe=True,
        install_guards=lambda *args: None,
        build_launch=lambda task, worktree, *args, **kwargs: SimpleNamespace(cwd=worktree),
    )

    def run(launch, timeout):
        (Path(launch.cwd) / "allowed.txt").write_text("CONTROL_OK")
        return 0, json.dumps(response)

    monkeypatch.setattr(probe, "_run_capture", run)
    measured = probe.probe_functional(adapter, Installation("test", "fake", "1"), CapabilitySet("test"))
    assert not measured.has("prewrite_file_guard")
    assert not measured.has("shell_guard")
    assert not measured.has("workspace_sandbox")


@pytest.mark.parametrize("capability,tool,tool_input", [
    ("prewrite_file_guard", "Write", {"file_path": "C:/fixture/guarded.txt", "content": "BREACHED"}),
    ("prewrite_file_guard", "Edit", {"file_path": "guarded.txt", "new_string": "BREACHED"}),
    ("shell_guard", "Bash", {"command": "echo BREACHED > guarded.txt"}),
    ("shell_guard", "PowerShell", {"command": "Set-Content guarded.txt BREACHED"}),
])
def test_guard_requires_correlated_tool_error(capability, tool, tool_input):
    from agentkit.probe_evidence import guard_denial

    invocation = {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": tool, "id": "negative", "input": tool_input,
    }]}}
    result = {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "negative", "is_error": True,
        "content": "[AgentKit] Edit blocked. guarded.txt is outside the task lease",
    }]}}
    stream = json.dumps(invocation) + "\n" + json.dumps(result)
    assert guard_denial(stream, capability, "C:/fixture/guarded.txt")
    result["message"]["content"][0]["tool_use_id"] = "unrelated"
    assert not guard_denial(json.dumps(invocation) + "\n" + json.dumps(result), capability, "C:/fixture/guarded.txt")
    assert not guard_denial(json.dumps(result), capability, "C:/fixture/guarded.txt")
    result["message"]["content"][0]["tool_use_id"] = "negative"
    result["message"]["content"][0]["is_error"] = False
    assert not guard_denial(json.dumps(invocation) + "\n" + json.dumps(result), capability, "C:/fixture/guarded.txt")


@pytest.mark.parametrize("tool_input", [
    {"file_path": "allowed.txt", "content": "guarded.txt"},
    {"file_path": "guarded.txt.backup"},
    {"file_path": "parent-guarded.txt"},
    {"file_path": "C:/wrong/guarded.txt"},
    None,
])
def test_other_file_denial_does_not_certify_negative_control(tool_input):
    from agentkit.probe_evidence import guard_denial

    stream = "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "id": "t", "input": tool_input}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": "[AgentKit] Edit blocked: guarded.txt"}]}},
    ])
    assert not guard_denial(stream, "prewrite_file_guard", "guarded.txt")


def test_bad_stream_is_inconclusive():
    from agentkit.probe_evidence import guard_denial

    assert not guard_denial('null\n[]\ninvalid\n{"message":"bad"}', "shell_guard", "guarded.txt")


def test_claude_guards_include_powershell(tmp_path):
    from agentkit.adapters.claude_code import ClaudeCodeAdapter

    ClaudeCodeAdapter().install_guards(tmp_path, {"id": 1}, tmp_path)
    settings = json.loads((tmp_path / ".claude/settings.local.json").read_text())
    for event in ("PreToolUse", "PostToolUse"):
        assert any("PowerShell" in group["matcher"] for group in settings["hooks"][event])


def test_legacy_positive_cache_is_demoted_without_probe(tmp_path):
    from agentkit.capabilities import load_cache, save_cache

    measured = CapabilitySet("test")
    for name in ("workspace_sandbox", "prewrite_file_guard", "shell_guard"):
        measured.set(name, True, "functional probe: blocked as expected")
    save_cache(tmp_path, {"test": measured})
    loaded = load_cache(tmp_path)["test"]
    assert not loaded.has("write_worker_safe")
    assert not loaded.has("prewrite_file_guard")
    assert not loaded.has("shell_guard")
    assert "legacy" in loaded.notes["workspace_sandbox"]


@pytest.mark.parametrize("command,event_type,code,expected", [
    ("Set-Content C:/fixtures/outside.txt BREACHED", "item.completed", 1, True),
    ("Set-Content C:/fixtures/unrelated.txt BREACHED", "item.completed", 1, False),
    ("Set-Content C:/fixtures/outside.txt BREACHED", "item.started", 1, False),
    ("Set-Content C:/fixtures/outside.txt BREACHED", "item.completed", 0, False),
    ("Set-Content C:/fixtures/outside.txt.backup BREACHED", "item.completed", 1, False),
    ("Get-Content C:/fixtures/outside.txt", "item.completed", 1, False),
    ("Set-Content outside.txt BREACHED", "item.completed", 1, False),
    ('Write-Output "Set-Content C:/fixtures/outside.txt BREACHED"', "item.completed", 1, False),
    ("Write-Output C:/fixtures/outside.txt; Get-Content secrets.txt", "item.completed", 1, False),
    ('"c:\\\\windows\\\\powershell.exe" -Command "Set-Content -LiteralPath \'C:\\\\fixtures\\\\outside.txt\' -Value \'BREACHED\' -NoNewline"', "item.completed", 1, True),
])
def test_sandbox_denial_requires_exact_completed_negative_command(command, event_type, code, expected):
    from agentkit.probe_evidence import sandbox_denial

    event = {"type": event_type, "item": {"type": "command_execution", "command": command,
             "exit_code": code, "aggregated_output": "C:/fixtures/outside.txt: Access is denied"}}
    assert sandbox_denial(json.dumps(event), "C:/fixtures/outside.txt") is expected


def test_application_directory_permission_is_not_os_confinement():
    from agentkit.probe_evidence import sandbox_denial

    stream = "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "PowerShell", "id": "negative", "input": {"command": "Set-Content C:/fixtures/outside.txt BREACHED"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "negative", "is_error": True, "content": "The command was blocked. May only access files in the allowed working directories"}]}},
    ])
    assert not sandbox_denial(stream, "C:/fixtures/outside.txt")
