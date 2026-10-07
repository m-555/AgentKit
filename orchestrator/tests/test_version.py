"""Catch stale editable installs and drift between release manifests."""
import json
import tomllib
from importlib import metadata
from pathlib import Path

from agentkit import __version__, version


def test_version_exposes_stale_install(monkeypatch):
    monkeypatch.setattr(metadata, "version", lambda package: "0.1.0")
    assert version.report() == (
        f"agentkit {__version__} (installed metadata 0.1.0; reinstall this checkout)"
    )


def test_source_checkout_without_metadata(monkeypatch):
    def absent(package):
        raise metadata.PackageNotFoundError(package)
    monkeypatch.setattr(metadata, "version", absent)
    assert "package metadata unavailable" in version.report()


def test_release_manifests_use_source_version():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "orchestrator/pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "agentkit/__init__.py"
    manifest = json.loads((root / "plugins/agentkit/.claude-plugin/plugin.json").read_text())
    assert manifest["version"] == __version__
