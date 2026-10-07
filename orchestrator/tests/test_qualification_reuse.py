"""Changing one provider must not remeasure another's identical guard implementation."""
import subprocess
from types import SimpleNamespace

import pytest

from agentkit import capabilities, qualification_reuse, runtime_identity
from agentkit.adapters.base import Installation


def fixture(tmp_path, monkeypatch, provider):
    root = tmp_path / "bundle"
    base = root / "orchestrator/agentkit"
    base.mkdir(parents=True)
    for name in (*runtime_identity.LEGACY_NAMES, "guard_files.py"):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("guard " + name + "\n").encode())
    for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "fixture"), ("config", "core.autocrlf", "false"),
                 ("add", "."), ("commit", "-qm", "qualified guard bundle")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    monkeypatch.setattr(runtime_identity, "__file__", str(base / "runtime_identity.py"))
    old = runtime_identity.digest_sources(runtime_identity.LEGACY_NAMES,
                                         lambda name: (base / name).read_bytes())
    identity = {"path": "fixture-runtime", "source": "fixture", "sha256": "binary", "enforcement": old}
    measured = capabilities.CapabilitySet(adapter=provider, version="1", installation=dict(identity))
    measured.probed_at = "2026-10-04T00:00:00+00:00"
    for field in ("workspace_sandbox", "prewrite_file_guard", "shell_guard"):
        measured.set(field, True, "functional v2: measured")
    capabilities.save_cache(root, {provider: measured})
    identity["enforcement"] = runtime_identity.enforcement_digest(provider)
    install = Installation(provider, "fixture-runtime", "1", "fixture", identity)
    adapter = SimpleNamespace(name=provider, detect=lambda: install)
    return root, base, revision, adapter


@pytest.mark.parametrize("provider,changed,allowed", [
    ("claude-code", "adapters/codex.py", True),
    ("codex", "wsl_session.py", True),
    ("claude-code", "hooks_cli.py", False),
    ("codex", "hooks_cli.py", False),
    ("codex", "guard_files.py", False),
    ("claude-code", "wsl_host.py", False),
])
def test_provider_specific_equivalence(tmp_path, monkeypatch, provider, changed, allowed):
    root, base, revision, adapter = fixture(tmp_path, monkeypatch, provider)
    (base / changed).write_bytes(b"changed guard\n")
    adapter.detect().identity["enforcement"] = runtime_identity.enforcement_digest(provider)
    if allowed:
        evidence = qualification_reuse.unchanged(root, adapter, revision)
        assert evidence["model_calls"] == 0
        assert capabilities.load_cache(root)[provider].probed_at == "2026-10-04T00:00:00+00:00"
        assert capabilities.load_cache(root)[provider].installation == adapter.detect().identity
    else:
        before = capabilities.load_cache(root)[provider].installation
        with pytest.raises(ValueError, match="Relevant guard changed"):
            qualification_reuse.unchanged(root, adapter, revision)
        assert capabilities.load_cache(root)[provider].installation == before


def test_new_binary_never_reuses_measurement(tmp_path, monkeypatch):
    root, _, revision, adapter = fixture(tmp_path, monkeypatch, "codex")
    adapter.detect().identity["sha256"] = "different"
    with pytest.raises(ValueError, match="Executable, host or transport changed"):
        qualification_reuse.unchanged(root, adapter, revision)


def test_unbound_revision_and_worker_cannot_certify(tmp_path, monkeypatch):
    root, _, revision, adapter = fixture(tmp_path, monkeypatch, "codex")
    with pytest.raises(ValueError, match="fingerprint"):
        qualification_reuse.unchanged(root, adapter, "a" * 40)
    monkeypatch.setenv("AGENTKIT_TASK", "1")
    with pytest.raises(PermissionError, match="Workers cannot"):
        qualification_reuse.unchanged(root, adapter, revision)
