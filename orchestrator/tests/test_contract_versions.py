"""Metadata-only freezes preserve reviewed work; real shape changes never do."""
import json

import pytest

from agentkit import (
    contract_versions,
    contracts,
    db,
    gates,
    integration_retry,
    integrator,
    repo,
)
from tests.conftest import commit_all
from tests.test_workflow import reviewed_change


def frozen_change(conn, project, project_root):
    owner = db.create_task(conn, spec_id="contract", title="Freeze", kind="CONTRACT_CHANGE",
                           status="DONE", expected_write=["contracts/api.yaml"])
    contracts.freeze(conn, project_root, project, owner, record=False)
    commit_all(project_root, "Freeze initial contract")
    identifier, work, head = reviewed_change(conn, project, project_root)
    db.update_task(conn, identifier, contract_version=1)
    target = integrator.integration_worktree(project)
    contracts.freeze(conn, target, project, owner, version=2, record=False)
    commit_all(target, "Advance metadata without changing interface")
    return identifier, work, target, head


def test_identical_freeze_merges_through_full_gate_without_worker(conn, project, project_root, monkeypatch):
    identifier, work, target, head = frozen_change(conn, project, project_root)
    seen = []
    def run(project, level, **kwargs):
        seen.append(level)
        return gates.GateResult(level, True)
    monkeypatch.setattr(gates, "run_gate", run)
    assert integrator.merge_one(conn, project, db.get_task(conn, identifier)).ok
    assert seen == ["full"]
    assert repo.head_commit(work) == head
    assert repo.is_ancestor(project_root, head, repo.current_branch(target))
    assert db.get_task(conn, identifier)["status"] == "DONE"
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["bytes", "added", "removed", "missing_lock", "wrong_pin", "empty", "target_drift", "worker_drift"])
def test_real_or_unproven_shape_changes_are_refused(conn, project, project_root, change):
    identifier, work, target, _ = frozen_change(conn, project, project_root)
    owner = db.get_task_by_spec(conn, "contract")["id"]
    if change == "bytes":
        (target / "contracts/api.yaml").write_text("version: 2\n")
    elif change == "added":
        (target / "contracts/new.yaml").write_text("version: 1\n")
    elif change == "removed":
        (target / "contracts/api.yaml").unlink()
    if change in ("bytes", "added", "removed"):
        commit_all(target, "Actual shape change")
        contracts.freeze(conn, target, project, owner, version=2, record=False)
        commit_all(target, "Freeze changed shape")
    elif change == "missing_lock":
        contracts.lock_path(work).unlink()
    elif change == "wrong_pin":
        db.update_task(conn, identifier, contract_version=3)
    elif change == "empty":
        for root in (work, target):
            path = contracts.lock_path(root)
            data = json.loads(path.read_text())
            data["paths"] = {}
            path.write_text(json.dumps(data))
    elif change == "target_drift":
        (target / "contracts/api.yaml").write_text("version: 9\n")
    elif change == "worker_drift":
        (work / "contracts/api.yaml").write_text("version: 9\n")
    assert not contract_versions.compatible(db.get_task(conn, identifier), project, target)
    db.set_status(conn, identifier, "FAILED", actor="integrator", cause="contract failure")
    db.update_task(conn, identifier, blocker="contract: frozen contract changed or version is stale")
    with pytest.raises(ValueError):
        integration_retry.authorize(conn, project, identifier, "A label alone cannot release this task")
    assert db.get_task(conn, identifier)["status"] == "FAILED"


def test_old_stale_failure_can_retry_same_reviewed_commit(conn, project, project_root):
    identifier, work, _, head = frozen_change(conn, project, project_root)
    db.set_status(conn, identifier, "FAILED", actor="integrator", cause="version counter advanced")
    db.update_task(conn, identifier, blocker="contract: frozen contract changed or version is stale")
    integration_retry.authorize(conn, project, identifier, "All frozen hashes, file sets and bytes identical")
    task = db.get_task(conn, identifier)
    assert task["status"] == "INTEGRATION_READY"
    assert task["generation"] == 0 and task["contract_version"] == 1
    assert repo.head_commit(work) == head
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
