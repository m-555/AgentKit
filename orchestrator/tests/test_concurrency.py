"""Unlimited-by-default concurrency, bounded by the graph and its safety checks."""
from __future__ import annotations

import argparse

import pytest

from agentkit import adapters, cli, concurrency, db, processes, scheduler, service
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import ProjectConfig


@pytest.fixture
def launcher(project_root, monkeypatch):
    caps = CapabilitySet(adapter="codex")
    for key in caps.values:
        caps.set(key, True)
    save_cache(project_root, {"codex": caps})
    monkeypatch.setattr(adapters.get("codex"), "detect", lambda: None)
    started = []

    def start(connection, root, built, **fields):
        started.append(fields["task_id"])
        return connection.execute("INSERT INTO processes(purpose,provider,task_id,status,launch_json,started_at) "
                                  "VALUES('worker',?,?,'RUNNING','{}',?)",
                                  (fields["provider"], fields["task_id"], db.utcnow())).lastrowid
    monkeypatch.setattr(processes, "start", start)
    return started


def ready(conn, name, path, **extra):
    return db.create_task(conn, spec_id=name, title=name, status="READY", expected_write=[path],
                          owned_paths=[path], **extra)


@pytest.mark.parametrize("value,expected", [(0, None), ("0", None), (3, 3), ("12", 12)])
def test_zero_is_unlimited_and_positive_is_a_cap(value, expected):
    assert concurrency.resolve(ProjectConfig(root="."), value) == expected


@pytest.mark.parametrize("value", [-1, "-2", "two", 1.5, True])
def test_negative_or_malformed_limits_are_rejected(value):
    with pytest.raises(ValueError):
        concurrency.validate(value)


def test_project_config_supplies_the_default(tmp_path):
    assert concurrency.resolve(ProjectConfig(root=tmp_path), None) is None
    assert concurrency.resolve(ProjectConfig(root=tmp_path, raw={"max_workers": 4}), None) == 4
    assert concurrency.resolve(ProjectConfig(root=tmp_path, raw={"concurrency": {"max_workers": 2}}), None) == 2
    assert concurrency.resolve(ProjectConfig(root=tmp_path, raw={"max_workers": 4}), 0) is None
    with pytest.raises(ValueError, match="negative"):
        concurrency.resolve(ProjectConfig(root=tmp_path, raw={"max_workers": -1}), None)


def test_cli_and_service_parse_the_same_contract():
    parser = cli.build_parser()
    assert parser.parse_args(["run"]).max_workers is None
    assert parser.parse_args(["run", "--max-workers", "0"]).max_workers == 0
    assert parser.parse_args(["job", "start", "--max-workers", "5"]).max_workers == 5
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--max-workers", "-1"])
    with pytest.raises(argparse.ArgumentTypeError):
        concurrency.argument("-3")
    assert service.parse_cap(service.FROM_CONFIG) is None
    assert service.parse_cap("0") == 0 and service.parse_cap("7") == 7
    with pytest.raises(ValueError):
        service.start(".", -1)


def test_unlimited_launches_every_independent_task_once(project_root, conn, launcher):
    ids = [ready(conn, f"t{i}", f"services/part{i}.py") for i in range(6)]
    report = scheduler.run_once(project_root)
    assert sorted(p.task["id"] for p in report.launched) == ids and sorted(launcher) == ids
    again = scheduler.run_once(project_root)
    assert again.launched == [] and sorted(launcher) == ids, "no replacement sessions for running work"


def test_positive_cap_limits_a_pass(project_root, conn, launcher):
    for i in range(5):
        ready(conn, f"t{i}", f"services/part{i}.py")
    assert len(scheduler.run_once(project_root, max_workers=2).launched) == 2
    assert scheduler.run_once(project_root, max_workers=2).launched == []
    assert len(scheduler.run_once(project_root, max_workers=0).launched) == 3


def test_overlapping_ready_tasks_never_launch_together(project_root, conn, launcher):
    """Unlimited mode removes the slot cap, never the overlap and lease checks.

    Independent READY tasks that declare the same file are a planning error: each
    protects its declared scope, so neither starts and the conflict is reported
    for the coordinator to sequence with depends_on.
    """
    first = ready(conn, "first", "services/retry.py")
    second = ready(conn, "second", "services/retry.py")
    report = scheduler.run_once(project_root)
    assert len(report.launched) <= 1 and len(launcher) <= 1
    assert any(task["id"] == second for task, _ in report.deferred)
    assert {int(lease["task_id"]) for lease in db.active_leases(conn)} <= {first}
    blocked = db.get_task(conn, first)
    assert "lease conflict" in (blocked["blocker"] or "") and str(second) in blocked["blocker"]
    assert scheduler.run_once(project_root).launched == []
    db.update_task(conn, second, depends_on=["first"])
    db.set_status(conn, second, "PLANNED", cause="coordinator sequenced the overlap")
    db.update_task(conn, first, blocker=None)
    assert [p.task["id"] for p in scheduler.run_once(project_root).launched] == [first]


def test_dependencies_and_gpu_slot_still_bound_unlimited_mode(project_root, conn, launcher):
    base = ready(conn, "base", "services/base.py")
    dependent = db.create_task(conn, spec_id="dependent", title="dependent", status="PLANNED",
                               expected_write=["services/dependent.py"], depends_on=["base"])
    report = scheduler.run_once(project_root)
    assert [p.task["id"] for p in report.launched] == [base]
    assert db.get_task(conn, dependent)["status"] == "PLANNED"
