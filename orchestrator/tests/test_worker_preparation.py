"""Preparation failure is a host concern and cannot consume a worker generation."""
import pytest

from agentkit import db, worker_preparation


def task(conn):
    return db.get_task(conn, db.create_task(conn, title="bounded", role="backend-builder",
        expected_read=["services/media.py"], expected_write=["new/package/result.py"]))


def test_host_creates_output_parents_without_creating_source(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    current = task(conn)
    worker_preparation.prepare(conn, project, current, project.root)
    assert (project.root / "new/package").is_dir()
    assert not (project.root / "new/package/result.py").exists()
    assert db.get_task(conn, current["id"])["generation"] == 0
    assert db.recent_events(conn, current["id"], kind="worker_files_prepared")


def test_missing_input_is_refused_before_any_folder_or_generation(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    current = task(conn)
    current["expected_read"] = ["missing.py"]
    with pytest.raises(ValueError, match="manager must repair"):
        worker_preparation.prepare(conn, project, current, project.root)
    assert not (project.root / "new").exists()
    assert db.get_task(conn, current["id"])["generation"] == 0


def test_escaping_path_refused_before_preparation(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    current = task(conn)
    current["expected_write"] = ["../outside.py"]
    with pytest.raises(ValueError, match="escapes checkout"):
        worker_preparation.prepare(conn, project, current, project.root)


def test_legacy_workflow_keeps_its_existing_scope_rules(conn, project):
    current = task(conn)
    current["expected_read"] = ["services/**"]
    worker_preparation.prepare(conn, project, current, project.root)
    assert not (project.root / "new").exists()


def test_operator_config_lease_does_not_consume_worker_capacity(conn, project, monkeypatch):
    from agentkit import models, operator, scheduler
    operator.acquire(conn, [".ai/project.yaml"], reason="configure project")
    for name in ("left", "right"):
        db.create_task(conn, title=name, spec_id=name, status="READY",
                       expected_write=[f"services/{name}.py"])
    selected = models.PROFILES["sol"]
    monkeypatch.setattr(models, "choose_worker", lambda *a, **k: (selected, "fixture"))
    plans, report = scheduler.plan(conn, project.root, project, max_workers=2)
    assert len(plans) == 2
    assert not report.deferred


def test_successful_build_cannot_hide_missing_deliverables(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    current = task(conn)
    with pytest.raises(ValueError, match="deliverables"):
        worker_preparation.validate_completed(project, current, project.root)
    target = project.root / "new/package/result.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n")
    worker_preparation.validate_completed(project, current, project.root)


def test_role_cap_does_not_leave_frontend_slot_empty(conn, project, monkeypatch):
    from agentkit import models, scheduler
    project.raw["workflow"] = {"mode": "separate-tasks", "worker_slots": {"backend-builder": 1, "frontend-builder": 1}}
    for title, role in [("backend-one", "backend-builder"), ("backend-two", "backend-builder"), ("frontend", "frontend-builder")]:
        db.create_task(conn, title=title, spec_id=title, status="READY", role=role,
                       owned_paths=[f"services/{title}.py"], expected_write=[f"services/{title}.py"])
    monkeypatch.setattr(models, "choose_worker", lambda *a, **k: (models.PROFILES["sol"], "fixture"))
    plans, report = scheduler.plan(conn, project.root, project, max_workers=2)
    assert [p.task["title"] for p in plans] == ["backend-one", "frontend"]
    assert any(task["title"] == "backend-two" and "role slots" in reason for task, reason in report.deferred)


def test_worker_packet_explains_supported_reads_without_weakening_guard():
    from agentkit.scheduler import WORKER_PROMPT
    assert "Get-Content -LiteralPath PATH" in WORKER_PROMPT
    assert "Never combine reads" in WORKER_PROMPT
    assert "Skills are already included" in WORKER_PROMPT
