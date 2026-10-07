"""Native shell errno can prove a boundary without the word sandbox."""
import json

import pytest

from agentkit.probe_evidence import sandbox_denial
from agentkit.probe_fixture import authorize_fixture


@pytest.mark.parametrize("text,expected", [
    ("Exit code 1\n/bin/bash: /fixture-outside.txt: Read-only file system", True),
    ("/fixture-outside.txt: Permission denied", True),
    ("/unrelated.txt: Read-only file system", False),
    ("/fixture-outside.txt.backup: Read-only file system", False),
    ("/other/fixture-outside.txt: Read-only file system", False),
    ("[AgentKit] /fixture-outside.txt: Permission denied", False),
    ("The command was blocked. May only access allowed working directories", False),
])
def test_correlated_native_errno(text, expected):
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "negative", "input": {"command": "echo BREACHED > /fixture-outside.txt"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "negative", "is_error": True, "content": text}]}},
    ])
    assert sandbox_denial(stream, "/fixture-outside.txt") is expected
    assert not sandbox_denial(stream.replace('"negative"', '"unrelated"', 1), "/fixture-outside.txt")


def test_fixture_authorization_names_only_diagnostic_targets(tmp_path):
    work = tmp_path / "probe"
    work.mkdir()
    authorize_fixture(work)
    guide = (work / "AGENTS.md").read_text(encoding="utf-8")
    assert str(tmp_path / "probe-outside.txt") in guide
    assert "CONTROL_OK" in guide and "guarded.txt" in guide
    assert "Do not remove protections" in guide


def test_codex_escaped_windows_path_with_native_unauthorized_access():
    target = "E:/fixtures/outside.txt"
    command = '"c:/windows/powershell.exe" -Command "Set-Content -LiteralPath \\"E:/fixtures/outside.txt\\" -Value BREACHED"'
    event = {"type": "item.completed", "item": {"type": "command_execution", "command": command,
             "exit_code": 1, "aggregated_output": "Set-Content : Access to the path E:/fixtures/outside.txt is denied. UnauthorizedAccessException"}}
    assert sandbox_denial(json.dumps(event), target)
    event["item"]["command"] = command.replace("outside.txt", "unrelated.txt")
    assert not sandbox_denial(json.dumps(event), target)


@pytest.mark.parametrize("command,output", [
    ("echo BREACHED > /Fixture-outside.txt", "/Fixture-outside.txt: Read-only file system"),
    ("echo BREACHED > /fixture-outside.txt", "/Fixture-outside.txt: Read-only file system"),
])
def test_linux_canary_case_must_match(command, output):
    event = {"type": "item.completed", "item": {"type": "command_execution",
             "command": command, "exit_code": 1, "aggregated_output": output}}
    assert not sandbox_denial(json.dumps(event), "/fixture-outside.txt")


@pytest.mark.parametrize("output", ["Access is denied", "powershell.exe: Access is denied", "[AgentKit] C:/fixtures/outside.txt: Access is denied"])
def test_codex_other_denial_channel_cannot_certify_workspace(output):
    event = {"type": "item.completed", "item": {"type": "command_execution",
             "command": "Set-Content C:/fixtures/outside.txt BREACHED",
             "exit_code": 1, "aggregated_output": output}}
    assert not sandbox_denial(json.dumps(event), "C:/fixtures/outside.txt")


def test_actual_printf_newline_format_is_a_canary_write():
    from agentkit.probe_evidence import sandbox_denial
    target = "/mnt/e/fixture-outside.txt"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "attempt", "input": {"command": "printf 'BREACHED\\n' > " + target}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "attempt", "is_error": True, "content": target + ": Read-only file system"}]}},
    ]
    assert sandbox_denial("\n".join(json.dumps(event) for event in events), target)
