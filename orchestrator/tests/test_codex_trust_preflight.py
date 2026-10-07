"""Exact owned hook metadata can authorize a launch; inferred trust cannot."""
import pytest

from agentkit import codex_trust
from agentkit.adapters.codex import _hook_command


def test_only_exact_owned_hook_is_trusted(monkeypatch, tmp_path):
    calls = []
    command = _hook_command("E:/python.exe", shell_kind="powershell")
    def rpc(binary, method, **kwargs):
        calls.append(kwargs)
        return {"entries": [{"hooks": [{"eventName": "preToolUse", "source": "sessionFlags", "matcher": "^(apply_patch|Bash)$", "command": command,
                 "key": "session:0:0", "currentHash": "sha256:123", "enabled": True,
                 "trustStatus": "trusted" if len(calls) == 2 else "untrusted"}]}]}
    monkeypatch.setattr(codex_trust, "rpc", rpc)
    flags = codex_trust.reviewed_flags("codex", tmp_path, command)
    assert len(calls) == 2 and 'trusted_hash="sha256:123"' in flags[-1]
    assert not any("dangerously" in value for value in flags)


@pytest.mark.parametrize("change", [{"enabled": False}, {"trustStatus": "untrusted"}, {"currentHash": "sha256:changed"}])
def test_changed_or_disabled_hook_withholds_launch(monkeypatch, tmp_path, change):
    command = _hook_command("E:/python.exe", shell_kind="powershell")
    calls = 0
    def rpc(*args, **kwargs):
        nonlocal calls
        calls += 1
        hook = {"eventName": "preToolUse", "source": "sessionFlags", "matcher": "^(apply_patch|Bash)$", "command": command, "key": "key", "currentHash": "sha256:original",
                "trustStatus": "trusted", "enabled": True}
        return [dict(hook, **change) if calls == 2 else hook]
    monkeypatch.setattr(codex_trust, "rpc", rpc)
    with pytest.raises(ValueError):
        codex_trust.reviewed_flags("codex", tmp_path, command)


def test_authority_evidence_matches_exact_target_and_layer():
    from agentkit.probe_evidence import authority_guard_denial
    rows = [{"layer": "L3", "channel": "tool:apply_patch", "path": "E:/fixture/guarded.txt"}]
    assert authority_guard_denial(rows, "prewrite_file_guard", "E:/fixture/guarded.txt")
    assert not authority_guard_denial(rows, "shell_guard", "E:/fixture/guarded.txt")
    assert not authority_guard_denial(rows, "prewrite_file_guard", "E:/other/guarded.txt")
