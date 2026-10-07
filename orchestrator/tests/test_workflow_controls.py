"""Local control isolation, profile application, job files and event dispatch."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection

import pytest

from agentkit import (
    dashboard,
    db,
    jobs,
    models,
    planner_events,
    scheduler,
    supervisor,
    workflow_setup,
)
from agentkit.config import load_project


def profile():
    return {"workflow": {"mode": "separate-tasks", "review": "human", "worker_slots": {
        "backend-builder": 2, "backend-tester": 2, "frontend-builder": 2, "frontend-tester": 2}}}


@contextmanager
def server(root, enabled=True):
    service = dashboard.make_server(root, 0, operator_controls=enabled)
    thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        service.shutdown()
        thread.join(3)
        service.server_close()


def request(service, route, method="GET", data=None, headers=None):
    client = HTTPConnection(*service.server_address, timeout=15)
    body = json.dumps(data) if data is not None else None
    client.request(method, route, body=body, headers=headers or {})
    response = client.getresponse()
    payload = response.read()
    client.close()
    try:
        result = json.loads(payload)
    except ValueError:
        result = {"message": payload.decode("utf-8")}
    return response.status, result


def test_controls_are_opt_in_and_cross_origin_requests_cannot_write(project_root):
    before = (project_root / ".ai/project.yaml").read_bytes()
    with server(project_root, False) as service:
        assert request(service, "/api/reviews")[1]["enabled"] is False
        assert request(service, "/api/team-profile", "POST", profile())[0] == 405
    with server(project_root) as service:
        info = request(service, "/api/reviews")[1]
        assert info["enabled"] and info["token"]
        assert request(service, "/api/team-profile", "POST", profile(), {"Content-Type": "application/json"})[0] == 403
        headers = {"Content-Type": "application/json", "X-AgentKit-Token": info["token"], "Origin": "https://foreign.example"}
        assert request(service, "/api/team-profile", "POST", profile(), headers)[0] == 403
    assert (project_root / ".ai/project.yaml").read_bytes() == before


def test_idle_project_profile_applies_without_starting_models_and_preserves_pause(project_root, conn):
    path = project_root / ".ai/project.yaml"
    path.write_text(path.read_text() + "\nexecution_paused: true\n")
    with server(project_root) as service:
        token = request(service, "/api/reviews")[1]["token"]
        headers = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        status, result = request(service, "/api/team-profile", "POST", profile(), headers)
        assert status == 200 and result["execution_paused"]
        assert request(service, "/api/start", "POST", {}, headers)[0] == 404
        assert request(service, "/api/team-profile", "POST", {"gates": {"full": ["evil"]}}, headers)[0] == 409
    assert load_project(project_root).raw["workflow"]["review"] == "human"
    assert conn.execute("SELECT COUNT(*) FROM processes").fetchone()[0] == 0


def test_unfinished_job_profile_cannot_be_changed(project_root):
    jobs.create(project_root, "existing", "Keep this job's policy")
    before = (project_root / ".ai/project.yaml").read_bytes()
    with pytest.raises(ValueError, match="unfinished"):
        workflow_setup.apply(project_root, profile())
    assert (project_root / ".ai/project.yaml").read_bytes() == before


def test_job_file_import_is_bounded_and_does_not_dispatch(project_root, conn):
    request = {"id": "submitted", "request": "Add one bounded feature", "acceptance": ["Preserve current behavior"]}
    job = workflow_setup.import_job(project_root, request)
    assert job["acceptance"] == request["acceptance"]
    assert conn.execute("SELECT COUNT(*) FROM processes").fetchone()[0] == 0
    for bad in ({**request, "id": "other", "acceptance": []},
                {**request, "id": "other", "request": "x" * 12001},
                {**request, "id": "other", "command": "start model"}):
        with pytest.raises(ValueError):
            workflow_setup.import_job(project_root, bad)
    assert not (project_root / ".ai/jobs/other.json").exists()


def stopped_control(conn, job_id):
    return conn.execute("INSERT INTO processes(purpose,job_id,provider,status,pid,child_pid,child_launch_state,launch_json,started_at,ended_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", ("coordinator", job_id, "codex", "FINISHED", 99999999, 99999998, "STARTED", "{}", db.utcnow(), db.utcnow())).lastrowid


def test_planner_is_called_once_for_unchanged_failure_and_again_for_new_request(conn, project_root, monkeypatch):
    path = project_root / ".ai/project.yaml"
    path.write_text(path.read_text() + "\nworkflow: {mode: separate-tasks, review: ai}\n")
    jobs.create(project_root, "planned", "Repair a failure")
    db.create_task(conn, title="Failed builder", spec_id="failed", job_id="planned", role="backend-builder",
                   expected_write=["services/retry.py"], status="FAILED", attempts=0, blocker="specific defect")
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    calls = []
    def control(*args, **kwargs):
        calls.append(args[3])
        return stopped_control(conn, "planned")
    monkeypatch.setattr(supervisor, "_control_launch", control)
    supervisor.tick(project_root)
    conn.execute("UPDATE jobs SET next_check=NULL")
    supervisor.tick(project_root)
    assert calls == ["coordinator"]
    jobs.amend(project_root, "planned", "Also preserve the labels", user=True)
    conn.execute("UPDATE jobs SET next_check=NULL")
    supervisor.tick(project_root)
    assert calls == ["coordinator", "coordinator"]


def test_worker_pool_cap_is_enforced_in_scheduler_plan(conn, project_root, monkeypatch):
    project = load_project(project_root)
    project.raw["workflow"] = {"mode": "separate-tasks", "review": "ai", "worker_slots": {"backend-builder": 1}}
    project.gates["source"] = ["echo source"]
    for index in range(3):
        db.create_task(conn, title=f"Builder {index}", spec_id=f"build-{index}", role="backend-builder",
                       expected_write=[f"services/unit_{index}.py"], status="READY", gate_level="source")
    chosen = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)
    monkeypatch.setattr(models, "choose_worker", lambda *args, **kwargs: (chosen, "qualified fixture"))
    plans, report = scheduler.plan(conn, project_root, project, max_workers=0)
    assert len(plans) == 1 and len(report.deferred) == 2
    assert all("role slots" in reason for _, reason in report.deferred)


def test_planner_records_no_dispatch_when_model_did_not_start(project_root, conn):
    project = load_project(project_root)
    project.raw["workflow"] = {"mode": "separate-tasks"}
    job = jobs.create(project_root, "waiting", "Wait for eligible provider")
    planner_events.record(conn, project, job, [], None)
    assert planner_events.should_dispatch(conn, project, job, [])
    assert conn.execute("SELECT COUNT(*) FROM planner_events").fetchone()[0] == 0


def test_http_human_approval_requires_exact_preview(conn, project_root, monkeypatch):
    from tests.test_review_workflow import complete
    _, packet = complete(project_root, conn, monkeypatch)
    with server(project_root) as service:
        info = request(service, "/api/reviews")[1]
        assert info["jobs"][0]["digest"] == packet["digest"]
        headers = {"Content-Type": "application/json", "X-AgentKit-Token": info["token"]}
        data = {key: packet[key] for key in ("job_id", "revision", "head", "digest")}
        data.update(verdict="PASS", evidence="Tested this exact feature preview")
        stale = {**data, "digest": "0" * 64}
        assert request(service, "/api/review-decision", "POST", stale, headers)[0] == 409
        assert conn.execute("SELECT COUNT(*) FROM human_acceptance").fetchone()[0] == 0
        status, result = request(service, "/api/review-decision", "POST", data, headers)
        assert status == 200 and result["status"] == "DONE"


def test_invalid_caps_and_unknown_review_mode_are_refused(project_root):
    from agentkit import review_policy
    project = load_project(project_root)
    for bad in (0, -1, True, 4, "2"):
        project.raw["workflow"] = {"worker_slots": {"backend-builder": bad}}
        with pytest.raises(ValueError):
            workflow_setup.worker_slots(project)
    project.raw["workflow"] = {"mode": "separate-tasks", "review": "manager"}
    with pytest.raises(ValueError, match="human or ai"):
        review_policy.mode(project)


def test_user_role_models_route_automatically_and_explicit_task_pin_wins(project_root):
    from agentkit import policy
    project = load_project(project_root)
    project.raw["workflow"] = {"mode": "separate-tasks", "review": "human"}
    project.raw["model_policy"] = {"assignments": {
        "backend-builder": {"profile": "opus", "model": "claude-opus-5-5", "effort": "high"},
        "override": {"profile": "sol", "model": "gpt-6.1-sol", "effort": "medium"}}}
    task = {"role": "backend-builder", "kind": "SAFE_PARALLEL", "complexity": "standard"}
    assert policy.for_task(project, task).primary.model == "claude-opus-5-5"
    assert policy.for_task(project, task).primary.effort == "high"
    assert policy.for_task(project, {**task, "model_assignment": "override"}).primary.effort == "medium"
    project.raw["workflow"] = {}
    assert policy.for_task(project, task) is None  # Legacy projects keep existing selection.


def test_planner_provider_failure_resumes_only_after_confirmed_recovery(project_root, conn):
    from datetime import UTC, datetime, timedelta

    from agentkit import providers

    project = load_project(project_root)
    project.raw["workflow"] = {"mode": "separate-tasks"}
    job = jobs.create(project_root, "limited", "Wait for allowance")
    identifier = stopped_control(conn, "limited")
    planner_events.record(conn, project, job, [], identifier)
    conn.execute("UPDATE processes SET status='FAILED',exit_code=1,error='Usage limit exceeded' WHERE id=?", (identifier,))
    now = datetime.now(UTC)
    providers.begin_cooldown(conn, "codex", reason="usage limit", retry_at=now - timedelta(seconds=1))
    assert not planner_events.should_dispatch(conn, project, job, [])  # Time alone is not proof.
    providers.observe(conn, "codex", {"available": True}, now=now + timedelta(minutes=1))
    assert planner_events.should_dispatch(conn, project, job, [])
