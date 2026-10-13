"""Discovery provenance must not invalidate a measured identical local executable."""
from types import SimpleNamespace

import pytest

from agentkit import capabilities
from agentkit.adapters.base import Installation


def check(tmp_path, observed, measured=None):
    original = {"path": "same-runtime", "source": "path", "sha256": "same-binary", "enforcement": "same-guards"}
    cached = capabilities.CapabilitySet(adapter="codex", version="1", installation=measured or original)
    capabilities.save_cache(tmp_path, {"codex": cached})
    install = Installation("codex", "same-runtime", "1", "extension", {**original, **observed})
    adapter = SimpleNamespace(name="codex", detect=lambda: install)
    return capabilities.cached_runtime_problem(tmp_path, adapter, "SAFE_PARALLEL")


def test_path_and_extension_identical_runtime_is_not_stale(tmp_path):
    assert check(tmp_path, {"source": "extension"}) is None


def test_extension_to_path_identical_runtime_is_not_stale(tmp_path):
    measured = {"path": "same-runtime", "source": "extension", "sha256": "same-binary", "enforcement": "same-guards"}
    assert check(tmp_path, {"source": "path"}, measured) is None


@pytest.mark.parametrize("changed", [
    {"path": "different-runtime"},
    {"sha256": "different-binary"},
    {"enforcement": "different-guards"},
    {"sha256": ""},
    {"source": "wsl"},
    {"host": "different-host"},
])
def test_security_identity_changes_still_require_measurement(tmp_path, changed):
    assert "requires a fresh" in check(tmp_path, {"source": "extension", **changed})
