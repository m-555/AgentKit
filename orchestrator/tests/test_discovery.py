"""Claude CLI discovery: newest compatible binary, honoured pins, no live requests."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentkit import adapters, db, discovery, models, scheduler, worktrees
from agentkit.capabilities import CapabilitySet, save_cache


@pytest.fixture
def installs(tmp_path, monkeypatch):
    """PATH holds 2.1.167; the VS Code extension ships 2.1.286 (and a stale 2.1.99)."""
    versions = {}

    def binary(path: Path, version: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("simulated", encoding="utf-8")
        versions[str(path)] = version
        return str(path)

    on_path = binary(tmp_path / "bin" / "claude.exe", "2.1.167")
    extensions = tmp_path / "extensions"
    current = binary(extensions / "anthropic.claude-code-2.1.286-win32-x64/resources/native-binary/claude.exe", "2.1.286")
    binary(extensions / "anthropic.claude-code-2.1.99-win32-x64/resources/native-binary/claude.exe", "2.1.99")
    state = {"path": on_path}
    monkeypatch.setattr(discovery, "_which", lambda name: state["path"])
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [extensions])
    monkeypatch.setattr(discovery, "_read_version", lambda path: versions[str(Path(path))])
    monkeypatch.delenv(discovery.PIN_ENV, raising=False)
    discovery._cache.clear()
    return {"path": on_path, "extension": current, "state": state, "binary": binary, "root": tmp_path}


def test_highest_version_beats_stale_path(installs):
    found = discovery.candidates()
    assert {c.version for c in found} == {"2.1.167", "2.1.286", "2.1.99"}
    selected = discovery.best(found)
    assert (selected.path, selected.version, selected.source) == (installs["extension"], "2.1.286", "extension")
    install = adapters.get("claude-code").detect()
    assert install.path == installs["extension"] and install.version == "2.1.286"
    assert adapters.get("claude-code").runtime_problem("claude-opus-5-5") is None


def test_old_path_only_gives_an_actionable_error(installs, monkeypatch):
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [])
    issue = adapters.get("claude-code").runtime_problem("claude-opus-5-5")
    assert "2.1.280" in issue and "2.1.167" in issue and discovery.PIN_ENV in issue
    assert "does not switch to another model" in issue
    assert adapters.get("claude-code").runtime_problem("claude-sonnet-5") is None


def test_pinned_cli_is_honoured_and_never_replaced(installs, monkeypatch):
    monkeypatch.setenv(discovery.PIN_ENV, installs["path"])
    install = adapters.get("claude-code").detect()
    assert (install.path, install.source) == (installs["path"], "configured")
    issue = adapters.get("claude-code").runtime_problem("claude-opus-5-5")
    assert "unset" in issue and "2.1.280" in issue
    monkeypatch.setenv(discovery.PIN_ENV, installs["extension"])
    assert adapters.get("claude-code").runtime_problem("claude-opus-5-5") is None
    monkeypatch.setenv(discovery.PIN_ENV, str(installs["root"] / "missing" / "claude.exe"))
    assert "does not exist" in adapters.get("claude-code").runtime_problem("claude-opus-5-5")


def test_unreadable_version_is_not_assumed_compatible(installs, monkeypatch):
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [])
    monkeypatch.setattr(discovery, "_read_version", lambda path: "unknown")
    assert "unreadable version" in adapters.get("claude-code").runtime_problem("claude-opus-5-5")


def test_build_launch_refuses_an_incompatible_cli(installs, monkeypatch, tmp_path):
    from agentkit.config import ProjectConfig
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [])
    project = ProjectConfig(root=tmp_path)
    selection = models.profile(project, "opus").to_dict()
    with pytest.raises(ValueError, match=r"2\.1\.280"):
        adapters.get("claude-code").build_launch({"id": 1, "_model_selection": selection}, tmp_path,
                                                 "implementer", project, prompt="Work")
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [installs["root"] / "extensions"])
    launch = adapters.get("claude-code").build_launch({"id": 1, "_model_selection": selection}, tmp_path,
                                                      "implementer", project, prompt="Work")
    assert launch.argv[0] == installs["extension"]
    assert launch.argv[launch.argv.index("--model") + 1] == "claude-opus-5-5"


def test_scheduler_refuses_before_taking_leases(installs, monkeypatch, project_root, conn, project):
    monkeypatch.setattr(discovery, "_extension_dirs", lambda: [])
    caps = CapabilitySet(adapter="claude-code")
    for key in caps.values:
        caps.set(key, True)
    save_cache(project_root, {"claude-code": caps})
    task_id = db.create_task(conn, title="Old CLI", spec_id="old-cli", status="READY",
                             expected_write=["services/retry.py"], model_profile="opus")
    task = db.get_task(conn, task_id)
    plan = scheduler.LaunchPlan(task, "claude-code", worktrees.path_for(project_root, task), 1, "test",
                                models.profile(project, "opus").to_dict())
    ok, detail = scheduler.launch(conn, project_root, project, plan)
    assert not ok and "2.1.280" in detail
    assert db.active_leases(conn) == []
    assert db.get_task(conn, task_id)["status"] == "READY"
