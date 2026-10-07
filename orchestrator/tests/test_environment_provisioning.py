"""Host readiness, invalidation, failure holds and warm reuse without AI calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentkit import environment_prepare as prepare
from agentkit import environment_profiles as profiles
from agentkit import gates
from agentkit.environment_capacity import reserve


def configure(project, commands=None):
    project.raw.update(environment_profiles={
        "static": {"setup": commands or [], "requires": [], "tools": [],
                   "min_free_bytes": 0, "reserve_bytes": 0}},
        environment_gates={"fast": "static", "full": "static"},
        dependency_cache_root=str(project.root.parent / "cache"))
    return project


def test_warm_readiness_skips_setup_and_invalidates_recipe(project, monkeypatch):
    configure(project, ["build"])
    calls = []
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: (calls.append(a[0]) or gates.CommandResult(a[0], 0, 0, "")))
    assert prepare.prepare(project, project.root, level="fast").passed
    assert prepare.prepare(project, project.root, level="fast").passed
    assert calls == ["build"]
    project.raw["environment_profiles"]["static"]["setup"] = ["new build"]
    assert prepare.prepare(project, project.root, level="fast").passed
    assert calls == ["build", "new build"]


def test_failure_is_durable_until_explicit_repair(project, monkeypatch):
    configure(project, ["broken"])
    calls = []
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: (calls.append(1) or gates.CommandResult(a[0], 1, 0, "deterministic")))
    first = prepare.prepare(project, project.root, level="fast")
    assert not first.passed
    assert not prepare.prepare(project, project.root, level="fast").passed
    assert len(calls) == 1
    assert prepare.hold(project, project.root, level="fast")
    prepare.repair(project, project.root)
    assert not prepare.prepare(project, project.root, level="fast").passed
    assert len(calls) == 2


def test_disk_failure_stops_then_capacity_recovery_unblocks(project, monkeypatch):
    configure(project, ["build"])
    profile = project.raw["environment_profiles"]["static"]
    profile.update(min_free_bytes=20, reserve_bytes=10)
    free = [5]
    monkeypatch.setattr(prepare.shutil, "disk_usage", lambda p: SimpleNamespace(free=free[0]))
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: gates.CommandResult(a[0], 0, 0, ""))
    assert not prepare.prepare(project, project.root, level="fast").passed
    assert not prepare.prepare(project, project.root, level="fast").passed
    free[0] = 2000000
    assert prepare.prepare(project, project.root, level="fast").passed


def test_cleaned_required_environment_invalidates_success(project):
    configure(project)
    project.raw["environment_profiles"]["static"]["requires"] = ["runtime.txt"]
    marker = project.root / "runtime.txt"
    marker.write_text("ready")
    assert prepare.prepare(project, project.root, level="fast").passed
    marker.unlink()
    assert not prepare.prepare(project, project.root, level="fast").passed


def test_wrong_profile_cannot_skip_javascript_gate(project):
    configure(project)
    project.gates["fast"] = ["npm test"]
    with pytest.raises(ValueError, match="does not cover"):
        profiles.select(project, level="fast")


def test_worker_cannot_prepare_or_clear_hold(project, monkeypatch):
    configure(project)
    monkeypatch.setenv("AGENTKIT_TASK", "1")
    with pytest.raises(PermissionError):
        prepare.prepare(project, project.root, level="fast")
    with pytest.raises(PermissionError):
        prepare.repair(project, project.root)


def test_capacity_reservations_are_released_and_count_other_preparations(project, monkeypatch):
    configure(project)
    monkeypatch.setattr(prepare.shutil, "disk_usage", lambda p: SimpleNamespace(free=100))
    profile = {"min_free_bytes": 20, "reserve_bytes": 50}
    with reserve(project, project.root, profile):
        with pytest.raises(OSError):
            with reserve(project, project.root, profile):
                pytest.fail("overcommitted capacity")
    with reserve(project, project.root, profile):
        pass


def test_static_profile_does_not_install_packages(project, monkeypatch):
    configure(project)
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: pytest.fail("unnecessary setup"))
    assert prepare.prepare(project, project.root, level="fast").passed
    receipt = json.loads(prepare.receipt_path(project, project.root).read_text())
    assert receipt["ai_calls"] == 0 and receipt["profile"] == "static"


def test_combined_profiles_reuse_components_without_duplicate_install(project, monkeypatch):
    configure(project)
    project.raw["environment_profiles"].update({
        "python": {"setup": ["prepare python"], "requires": [], "tools": []},
        "javascript": {"setup": ["prepare javascript"], "requires": [], "tools": []},
        "combined": {"setup": [], "components": ["python", "javascript"],
                     "requires": [], "tools": [], "reserve_bytes": 0}})
    project.raw["environment_gates"]["full"] = "combined"
    calls = []
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: (calls.append(a[0]) or gates.CommandResult(a[0], 0, 0, "")))
    assert prepare.prepare(project, project.root, profile_name="python").passed
    assert prepare.prepare(project, project.root, level="full").passed
    assert calls == ["prepare python", "prepare javascript"]
    project.raw["environment_profiles"]["python"]["setup"] = ["new python"]
    assert prepare.prepare(project, project.root, level="full").passed
    assert calls == ["prepare python", "prepare javascript", "new python"]


def test_component_cycles_are_rejected_before_any_setup(project):
    configure(project)
    project.raw["environment_profiles"]["static"]["components"] = ["static"]
    with pytest.raises(ValueError, match="cycle"):
        profiles.select(project, level="fast")


def test_real_host_setup_receipt_and_environment_injection(project):
    import sys
    command = '"' + sys.executable + '" -c "from pathlib import Path; Path(\'prepared.txt\').write_text(\'ready\')"'
    configure(project, [command])
    project.raw["environment_profiles"]["static"]["requires"] = ["prepared.txt"]
    first = prepare.prepare(project, project.root, level="fast")
    assert first.passed, first.summary()
    second = prepare.prepare(project, project.root, level="fast")
    assert second.passed and "Reused prepared" in second.summary()
    assert (project.root / "prepared.txt").read_text() == "ready"


def test_fingerprint_observes_untracked_root_and_tracked_nested_manifests(project):
    from conftest import git
    configure(project)
    child = project.root / "apps/front/package.json"
    child.parent.mkdir(parents=True)
    child.write_text('{"version":"1"}')
    git(project.root, "add", "apps/front/package.json")
    profile = profiles.select(project, level="fast")
    initial = profiles.fingerprint(project.root, profile)
    child.write_text('{"version":"2"}')
    changed = profiles.fingerprint(project.root, profile)
    assert changed != initial
    (project.root / "package-lock.json").write_text('{"lockfileVersion":3}')
    assert profiles.fingerprint(project.root, profile) != changed


def test_dependency_check_failure_blocks_ready_receipt(project, monkeypatch):
    configure(project)
    project.raw["environment_profiles"]["static"]["checks"] = ["import dependencies"]
    monkeypatch.setattr(gates, "_run_one", lambda *a, **k: gates.CommandResult(a[0], 1, 0, "missing dependency"))
    assert not prepare.prepare(project, project.root, level="fast").passed
    assert prepare.hold(project, project.root, level="fast")


def test_warm_receipt_rechecks_dependencies_without_installing(project, monkeypatch):
    configure(project, ["install"])
    project.raw["environment_profiles"]["static"]["checks"] = ["import dependencies"]
    calls = []
    broken = [False]
    def run(command, *args, **kwargs):
        calls.append(command)
        failed = broken[0] and command == "import dependencies"
        return gates.CommandResult(command, int(failed), 0, "missing dependency" if failed else "")
    monkeypatch.setattr(gates, "_run_one", run)
    assert prepare.prepare(project, project.root, level="fast").passed
    broken[0] = True
    assert not prepare.prepare(project, project.root, level="fast").passed
    assert calls == ["install", "import dependencies", "import dependencies"]
