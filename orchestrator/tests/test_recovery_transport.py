"""Runtime bridges preserve ownership and only acknowledge observed provider events."""
from __future__ import annotations

import json

import pytest

from agentkit import db, external_identity, providers, quota, recovery_runtime
from agentkit import recovery_store as store


def parked(conn, project_root, make_task):
    identifier = make_task("Preserved", ["services/media.py"], status="BLOCKED",
                           worktree=str(project_root), adapter="codex")
    task = db.get_task(conn, identifier)
    row = recovery_runtime.park_worker(conn, task, {"provider":"codex", "paused_at":"one"})
    providers.clear_cooldown(conn, "codex")
    return identifier, row


def test_cli_claim_generation_step_and_actual_receipts(conn, project_root, make_task):
    task, row = parked(conn, project_root, make_task)
    db.bump_generation(conn, task)
    db.update_task(conn, task, status="LEASED")
    claim = recovery_runtime.before_launch(conn, project_root, purpose="worker", provider="codex", task_id=task)
    assert claim
    # No real Popen; mimic the host's persisted monitor and provider stream.
    cursor=conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at) VALUES('worker','codex',?,1,'{}',?)", (task,db.utcnow()))
    process_id=cursor.lastrowid
    recovery_runtime.launched(conn, process_id, claim)
    assert store.get(conn, row["id"])["state"] == "DELIVERY_ACCEPTED"
    recovery_runtime.turn_started(conn, process_id)
    assert store.get(conn, row["id"])["state"] == "TURN_STARTED"
    recovery_runtime.finished(conn, project_root, dict(conn.execute("SELECT * FROM processes WHERE id=?",(process_id,)).fetchone()), 0, "")
    assert store.get(conn, row["id"])["state"] == "RECOVERED"
    assert db.get_task(conn, task)["status"] == "LEASED"  # Recovery is not task approval.


def test_cli_generation_drift_refuses_continuation(conn, project_root, make_task):
    task, row=parked(conn, project_root, make_task)
    db.bump_generation(conn,task)
    db.bump_generation(conn,task)
    with pytest.raises(ValueError,match="not ready"):
        recovery_runtime.before_launch(conn,project_root,purpose="worker",provider="codex",task_id=task)
    assert store.get(conn,row["id"])["state"] == "CANCELLED"


def test_live_owner_cannot_be_transferred(conn, project_root, make_task):
    task,row=parked(conn,project_root,make_task)
    conn.execute("INSERT INTO processes(purpose,provider,task_id,launch_json,started_at) VALUES('worker','codex',?,'{}',?)",(task,db.utcnow()))
    assert not recovery_runtime.authorize(conn,project_root,row)[0]


def test_model_fallback_claim_uses_target_account_and_preserves_bytes(conn,project_root,make_task):
    task,row=parked(conn,project_root,make_task)
    providers.begin_cooldown(conn,"codex",reason="quota")
    providers.clear_cooldown(conn,"claude-code")
    assert recovery_runtime.authorize(conn,project_root,row,provider="claude-code")[0]
    claim=recovery_runtime.before_launch(conn,project_root,purpose="worker",provider="claude-code",task_id=task)
    assert claim
    assert not recovery_runtime.authorize(conn,project_root,store.get(conn,row["id"]))[0]
    assert (project_root/'services/media.py').read_text() == "VALUE = 'media'\n"


def test_project_pause_cancels_pending_without_touching_worker_files(conn,project_root,make_task):
    _,row=parked(conn,project_root,make_task)
    config=project_root/'.ai/project.yaml'
    config.write_text(config.read_text()+"execution_paused: true\n")
    recovery_runtime.reconcile(conn,project_root)
    assert store.get(conn,row["id"])["state"] == "CANCELLED"


def test_local_capacity_pause_is_health_not_five_hour(conn,make_task):
    task=make_task("Future local",["services/media.py"])
    assert quota.handle_worker_failure(conn,task,"local-opencode","429 overloaded",1)
    current=db.get_task(conn,task)
    meta=json.loads(current['blocked_meta'])
    assert meta['reason'] == quota.HEALTH_REASON
    assert current['attempts'] == 0
    assert providers.get_state(conn,"local-opencode").seconds_remaining() <= 60
    assert store.snapshot(conn)[0]['policy'] == "unmetered"


@pytest.mark.parametrize("provider,variable",[("codex","CODEX_THREAD_ID"),
    ("claude-code","CLAUDE_CODE_SESSION_ID"),("local-opencode","AGENTKIT_EXTERNAL_SESSION_REF")])
def test_external_manager_identity_is_provider_specific(monkeypatch,provider,variable):
    for key in ("CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","CLAUDE_SESSION_ID","AGENTKIT_EXTERNAL_SESSION_REF"):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv(variable,"current")
    assert external_identity.require({'provider':provider,'session_ref':'current'}) == "current"
    with pytest.raises(PermissionError):
        external_identity.require({'provider':provider,'session_ref':'other'})
