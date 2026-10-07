"""Warm Python reuse installs pinned cached wheels into a fresh, private venv."""
from __future__ import annotations

import json
import sys

import pytest

from agentkit import environment_wheels as wheels


def test_warm_wheel_reuse_uses_final_environment_and_offline_install(project, monkeypatch):
    project.raw["dependency_cache_root"] = str(project.root.parent / "cache")
    key = "key"
    cached = wheels._directory(project, key)
    (cached / "wheels").mkdir(parents=True)
    (cached / "requirements.lock").write_text("example==1.2\n")
    (cached / "receipt.json").write_text(json.dumps({"key": key}))
    profile = {"python": "venv/bin/python"}
    calls = []
    monkeypatch.setattr(wheels, "_run", lambda argv, **kw: calls.append(argv))
    assert wheels.restore(project, project.root, profile, key)
    assert calls[0] == [sys.executable, "-m", "venv", str(project.root / "venv")]
    assert "--no-index" in calls[1]
    assert str(project.root / "venv/bin/python") == calls[1][0]


def test_editable_install_cannot_be_reused_as_wheels(project, monkeypatch):
    monkeypatch.setattr(wheels, "_run", lambda *a, **k: "-e /another/checkout")
    with pytest.raises(ValueError, match="editable"):
        wheels.capture(project, project.root, {"python":"venv/bin/python"}, "key")


def test_wheel_python_cannot_escape_private_environment(project):
    with pytest.raises(ValueError):
        wheels._python(project.root, {"python": "../operator/python"})
