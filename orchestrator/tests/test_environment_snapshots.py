"""Private dependency snapshots cannot retain links to another worker's source."""
from __future__ import annotations

import pytest

from agentkit import environment_snapshots as snapshots


def fixture(project, monkeypatch):
    work = project.root
    (work / "package-lock.json").write_text("{}")
    (work / "package.json").write_text("{}")
    (work / "apps/front").mkdir(parents=True)
    (work / "apps/front/package.json").write_text('{"name":"@repo/front"}')
    monkeypatch.setattr("agentkit.environment_profiles.inputs",
                        lambda p: ["package.json", "package-lock.json", "apps/front/package.json"])
    project.raw["dependency_cache_root"] = str(work.parent / "cache")
    modules = work / "node_modules"
    (modules / "library").mkdir(parents=True)
    (modules / "library/index.js").write_text("original")
    return work


def test_private_snapshot_reuses_preparation_without_shared_mutation(project, monkeypatch):
    work = fixture(project, monkeypatch)
    snapshots.capture(project, work, "key")
    other = work.parent / "other"
    (other / "apps/front").mkdir(parents=True)
    (other / "apps/front/package.json").write_text('{"name":"@repo/front"}')
    assert snapshots.restore(project, other, "key")
    (other / "node_modules/library/index.js").write_text("worker change")
    assert (work / "node_modules/library/index.js").read_text() == "original"
    assert (snapshots._snapshot(project, "key") / "node_modules/library/index.js").read_text() == "original"
    assert not snapshots.restore(project, other, "key")


def test_recognized_workspace_links_rebind_to_current_checkout(project, monkeypatch):
    work = fixture(project, monkeypatch)
    snapshots._link(work / "node_modules/@repo/front", work / "apps/front")
    snapshots.capture(project, work, "key")
    other = work.parent / "other"
    (other / "apps/front").mkdir(parents=True)
    (other / "apps/front/package.json").write_text('{"name":"@repo/front"}')
    assert snapshots.restore(project, other, "key")
    assert (other / "node_modules/@repo/front").resolve() == (other / "apps/front").resolve()


def test_foreign_dependency_links_are_refused(project, monkeypatch):
    work = fixture(project, monkeypatch)
    foreign = work.parent / "foreign"
    foreign.mkdir()
    snapshots._link(work / "node_modules/foreign", foreign)
    with pytest.raises(ValueError, match="unrecognized"):
        snapshots.capture(project, work, "key")


def test_snapshot_requires_lockfile_and_no_workspace_lifecycle_script(project, monkeypatch):
    work = fixture(project, monkeypatch)
    profile = {"strategy": "snapshot-copy", "setup": ["npm ci"]}
    assert snapshots.eligible(work, profile)
    (work / "apps/front/package.json").write_text('{"scripts":{"postinstall":"write source"}}')
    with pytest.raises(ValueError, match="lifecycle"):
        snapshots.eligible(work, profile)
    with pytest.raises(ValueError, match="compound"):
        snapshots.eligible(work, {"strategy":"snapshot-copy","setup":["npm ci && mutate"]})
