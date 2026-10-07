"""Guard results inspect bytes and accept native PowerShell control encoding."""
from types import SimpleNamespace

import pytest

from agentkit import probe
from agentkit.adapters.base import Installation
from agentkit.capabilities import CapabilitySet
from agentkit.probe_fixture import read_control


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be"])
def test_control_text_decodes_supported_native_boms(tmp_path, encoding):
    data = "CONTROL_OK".encode(encoding)
    if encoding == "utf-16-be":
        data = b"\xfe\xff" + data
    path = tmp_path / "allowed.txt"
    path.write_bytes(data)
    assert read_control(path) == "CONTROL_OK"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8"])
def test_any_unleased_byte_change_demotes_guard_without_decode_crash(monkeypatch, tmp_path, encoding):
    adapter = SimpleNamespace(
        supports_write_probe=True,
        install_guards=lambda *args: None,
        build_launch=lambda task, worktree, *args, **kwargs: SimpleNamespace(cwd=worktree),
    )
    calls = []

    def run(launch, timeout):
        calls.append(launch)
        (launch.cwd / "allowed.txt").write_bytes("CONTROL_OK".encode("utf-16"))
        if len(calls) <= 2:
            (launch.cwd / "guarded.txt").write_bytes("changed".encode(encoding))
        return 0, ""

    monkeypatch.setattr(probe, "_run_capture", run)
    result = probe.probe_functional(adapter, Installation("test", "fake", "1"),
                                    CapabilitySet("test"), fixture_root=tmp_path)
    assert len(calls) == 3
    assert not result.has("prewrite_file_guard")
    assert not result.has("shell_guard")
    assert "modified a file" in result.notes["shell_guard"]
