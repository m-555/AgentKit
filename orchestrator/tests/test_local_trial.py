"""F35: the user may trial a local, tool-confined writer on one HOTSPOT task.

On 2026-10-11 the user asked to give a pilot project's main.py wiring task (a HOTSPOT) to
the local Qwen builder to measure its reliability. Five checks refused it,
correctly by default: a local worker has no OS sandbox (F22). The manager now
records the user's decision for that one task; the task stays HOTSPOT for
leases, review and integration, and the local worker runs it under the
bounded-writer confinement every local writer gets. Decouple and contract work
are never eligible, and the approval lapses when the task's spec changes.
"""
from __future__ import annotations

import json
import os

import pytest

from agentkit import cli, db, jobs, local_trial, manager, models, policy
from agentkit.capabilities import CapabilitySet

QWEN = {"profile": "qwen", "model": "local/qwen3.8-27b-q8-tuber", "effort": None}
JOB = "trial-job"


def _trial(spec_hash="h1"):
    return json.dumps({"granted_by": "root", "granted_at": "2026-10-11T21:30:00+00:00",
                       "evidence": "The user asked for this trial.", "spec_hash": spec_hash})


def _task(kind="HOTSPOT", trial=True, **extra):
    return {"id": 1, "kind": kind, "complexity": "standard", "model_assignment": "qwen-builder",
            "role": "backend-builder", "spec_hash": "h1", "owned_paths": ["main.py"],
            "local_trial": _trial() if trial else None, **extra}


def _measured():
    caps = CapabilitySet(adapter="local-opencode")
    for name in ("resume_session", "structured_output", "mcp_stdio", "event_stream", "prewrite_file_guard",
                 "tool_only_confinement"):
        caps.set(name, True)
    return {"local-opencode": caps}


def test_a_granted_hotspot_admits_qwen_through_policy_and_worker_choice(project, conn):
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    task = _task()
    assert local_trial.granted(task) and models.small_model_fits("qwen", task)
    assert policy.for_task(project, task).primary.name == "qwen"
    candidate, reason = models.choose_worker(conn, project, task, _measured())
    assert candidate and candidate.name == "qwen", reason


@pytest.mark.parametrize("task", [_task(trial=False), _task(kind="DECOUPLE"), _task(kind="CONTRACT_CHANGE"),
                                  _task(spec_hash="h2"), _task(local_trial="not json"),
                                  _task(local_trial=json.dumps({"spec_hash": "h1", "evidence": " "}))])
def test_no_trial_another_kind_a_changed_spec_or_no_evidence_is_still_refused(project, conn, task):
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    assert not local_trial.granted(task)
    with pytest.raises(ValueError):
        policy.for_task(project, task)
    try:
        candidate, _ = models.choose_worker(conn, project, task, _measured())
    except ValueError:
        candidate = None
    assert candidate is None


def test_only_qwen_benefits_and_only_with_measured_confinement(project, conn):
    assert not models.small_model_fits("sonnet", _task())
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    unmeasured = _measured()
    unmeasured["local-opencode"].set("tool_only_confinement", False)
    candidate, _ = models.choose_worker(conn, project, _task(), unmeasured)
    assert candidate is None


def test_the_local_worker_runs_a_trial_hotspot_as_a_confined_builder(project, tmp_path, monkeypatch):
    from agentkit.adapters.local_opencode import WRITER_OUTPUT, LocalOpenCodeAdapter
    source = tmp_path / "local-opencode"
    source.mkdir()
    (source / "opencode.json").write_text(json.dumps({"provider": {"local": {
        "options": {"baseURL": "http://127.0.0.1:8080/v1"}, "models": {"qwen3.8-27b-q8-tuber": {}}}}}))
    monkeypatch.setenv("AGENTKIT_LOCAL_OPENCODE", str(source))
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    adapter = LocalOpenCodeAdapter()
    adapter.detect = lambda: None
    task = _task(title="Wire routers", expected_write=["main.py"], expected_read=["routes/a.py"])
    launch = adapter.build_launch(task, project.root, "backend-builder", project, prompt="Go")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    permission = config["permission"]
    assert permission["edit"]["*"] == "deny" and permission["edit"]["*main.py"] == "allow"
    assert permission["bash"] == "deny" and permission["external_directory"] == "deny"
    assert permission["read"]["*"] == "deny" and permission["agentkit_commit_and_gate"] == "allow"
    assert "Write only: main.py" in config["agent"]["agentkit-worker"]["prompt"]
    assert config["provider"]["local"]["models"]["qwen3.8-27b-q8-tuber"]["limit"]["output"] == WRITER_OUTPUT
    guards = adapter.install_guards(project.root, task, None)
    assert {"L5_host_monitor", "L6_pre_commit"} <= set(guards.active)
    with pytest.raises(ValueError):
        adapter.build_launch(_task(kind="DECOUPLE", expected_write=["main.py"]), project.root, "backend-builder",
                             project, prompt="Go")


@pytest.fixture
def attached(project_root, conn):
    jobs.create(project_root, JOB, "Wire the routers", "codex")
    token = manager.attach(conn, project_root, JOB, "root-chat", os.getpid(), session_ref="root")
    credential = project_root / ".ai" / "runtime" / f"manager-{JOB}.credential"
    credential.parent.mkdir(exist_ok=True)
    credential.write_text(token)
    task_id = db.create_task(conn, title="wire", spec_id="wire", kind="HOTSPOT", status="READY",
                             owned_paths=["main.py"], expected_write=["main.py"], job_id=JOB, spec_hash="h1")
    return token, task_id


def test_the_attached_manager_records_the_users_decision_for_one_task(conn, attached):
    token, task_id = attached
    value = local_trial.grant(conn, JOB, task_id, token, "The user asked to measure Qwen on this task.")
    assert value["granted_by"] == "root-chat" and value["spec_hash"] == "h1"
    task = db.get_task(conn, task_id)
    assert local_trial.granted(task)
    events = [row["kind"] for row in conn.execute("SELECT kind FROM events WHERE task_id=?", (task_id,))]
    assert "local_trial_granted" in events


@pytest.mark.parametrize("problem", ["token", "evidence", "kind", "job", "done"])
def test_a_trial_needs_the_manager_credential_evidence_and_an_open_hotspot_of_this_job(conn, attached, problem):
    token, task_id = attached
    evidence = "The user asked."
    if problem == "token":
        token = "forged"
    elif problem == "evidence":
        evidence = "  "
    elif problem == "kind":
        db.update_task(conn, task_id, kind="DECOUPLE")
    elif problem == "job":
        db.update_task(conn, task_id, job_id="another-job")
    else:
        conn.execute("UPDATE tasks SET status='DONE' WHERE id=?", (task_id,))
    with pytest.raises((PermissionError, ValueError)):
        local_trial.grant(conn, JOB, task_id, token, evidence)
    assert not local_trial.granted(db.get_task(conn, task_id))


def test_the_cli_reads_the_credential_and_evidence_file(project_root, conn, attached, tmp_path, capsys):
    _, task_id = attached
    evidence = tmp_path / "decision.txt"
    evidence.write_text("The user asked to measure Qwen on this task.", encoding="utf-8")
    assert cli.main(["--path", str(project_root), "manager", "local-trial", JOB, "--task", str(task_id),
                     "--evidence-file", str(evidence)]) == 0
    assert json.loads(capsys.readouterr().out)["local_trial"]["evidence"].startswith("The user asked")
