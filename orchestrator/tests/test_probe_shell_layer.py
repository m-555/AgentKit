"""Probe guard changes never affect a real project's settings."""
import json

import pytest

from agentkit.probe_shell_layer import measure_os_boundary


def test_only_fixture_command_guard_is_removed_and_exact_bytes_restored(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    settings = {"sandbox": {"enabled": True, "failIfUnavailable": True}, "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"command": "python -m agentkit.hooks_cli pre-bash"},
            {"command": "other-security-check"}]},
            {"matcher": "Write", "hooks": [{"command": "python -m agentkit.hooks_cli pre-tool-use"}]}],
        "PostToolUse": [{"hooks": [{"command": "audit"}]}]}}
    original = (json.dumps(settings, indent=2) + "\r\n").encode()
    path.write_bytes(original)
    with measure_os_boundary("claude-code", tmp_path):
        measured = json.loads(path.read_bytes())
        assert measured["sandbox"] == settings["sandbox"]
        assert measured["hooks"]["PreToolUse"][0]["hooks"] == [{"command": "other-security-check"}]
        assert measured["hooks"]["PreToolUse"][1] == settings["hooks"]["PreToolUse"][1]
        assert measured["hooks"]["PostToolUse"] == settings["hooks"]["PostToolUse"]
    assert path.read_bytes() == original
    with pytest.raises(RuntimeError), measure_os_boundary("claude-code", tmp_path):
        raise RuntimeError("failed probe")
    assert path.read_bytes() == original


def test_other_adapters_and_missing_fixture_do_not_change_settings(tmp_path):
    with measure_os_boundary("claude-code", tmp_path):
        assert not (tmp_path / ".claude").exists()
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text("unchanged")
    with measure_os_boundary("codex", tmp_path):
        assert path.read_text() == "unchanged"


def test_codex_os_measurement_restores_fixture_hooks_and_launch_after_error(tmp_path):
    from agentkit.adapters.base import Launch
    path = tmp_path / ".codex/hooks.json"
    path.parent.mkdir()
    original = b'{"hooks": {}}\r\n'
    path.write_bytes(original)
    argv = ["codex", "exec", "-c", "hooks.PreToolUse=[guard]", "-c", "hooks.state=[trust]",
            "-c", "sandbox_mode=workspace-write", "prompt"]
    launch = Launch(argv=list(argv))
    with pytest.raises(RuntimeError), measure_os_boundary("codex", tmp_path, launch=launch):
        assert not path.exists()
        assert "hooks.PreToolUse=[guard]" not in launch.argv
        assert "sandbox_mode=workspace-write" in launch.argv
        raise RuntimeError("fixture failure")
    assert launch.argv == argv
    assert path.read_bytes() == original
