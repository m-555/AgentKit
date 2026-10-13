"""F29: a lapsed manager chat hands the job to the coordinator, even while its process lives.

The VS Code manager chat stopped at a usage limit. Its process stayed alive, and
the external lease fenced the coordinator while the bridge PID lived, so after
the reset nobody resumed the work. Only a fresh heartbeat fences now. The
chat's own turns renew a fresh lease (the Stop and PreCompact hooks), the
returning chat waits while the coordinator manages, reviews left to an absent
external manager wake the coordinator, and the view says who manages.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    db,
    hooks_cli,
    jobs,
    manager,
    manager_chat,
    manager_presence,
    manager_state,
    models,
    supervisor,
)

JOB = "presence-job"
OPUS = models.Profile("opus", "claude-code", "claude-opus-5-5", "high", 2)
SESSION = "chat-session-1"


@pytest.fixture
def chat(project_root, conn, monkeypatch):
    """The manager chat attached with its own (live) process, as in VS Code."""
    for name in ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS"):
        monkeypatch.delenv(name, raising=False)
    config = project_root / ".ai" / "project.yaml"
    config.write_text(config.read_text() + "review_mode: external-manager\n")
    jobs.create(project_root, JOB, "Keep building while the manager chat sleeps", "claude-code")
    jobs.pin_coordinator(project_root, JOB, OPUS)
    token = manager.attach(conn, project_root, JOB, "vscode-chat", os.getpid(), session_ref=SESSION, ttl_seconds=3600)
    credential = project_root / ".ai" / "runtime" / f"manager-{JOB}.credential"
    credential.write_text(token)
    return token


def lapse(conn, hours=2):
    past = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (past, JOB))


def coordinator(conn, status="RUNNING"):
    return conn.execute("INSERT INTO processes(purpose,provider,job_id,status,pid,launch_json,started_at) "
                        "VALUES('coordinator','claude-code',?,?,?,'{}',?)",
                        (JOB, status, os.getpid(), db.utcnow())).lastrowid


def test_a_lapsed_lease_lets_the_coordinator_take_over_while_the_chat_process_lives(conn, chat):
    assert manager.blocks_spawn(conn, JOB)
    lapse(conn)
    assert manager.pid_alive(os.getpid())  # the chat's process is still alive
    assert not manager.blocks_spawn(conn, JOB)
    assert manager_state.pending(conn, JOB)  # the outage is recorded once for the coordinator to audit
    epoch = manager_state.state(conn, JOB)["epoch"]
    assert not manager.blocks_spawn(conn, JOB) and manager_state.state(conn, JOB)["epoch"] == epoch


def test_the_returning_chat_waits_while_the_coordinator_manages(project_root, conn, chat):
    lapse(conn)
    manager.blocks_spawn(conn, JOB)
    process = coordinator(conn)
    for call in (lambda: manager.heartbeat(conn, JOB, chat),
                 lambda: manager.handover(conn, JOB, chat, "next-chat", os.getpid(), session_ref="s2"),
                 lambda: manager.planner_authority(project_root, JOB)):
        with pytest.raises(PermissionError, match=f"process {process}"):
            call()


def test_after_the_coordinator_the_chat_renews_audits_and_its_next_lapse_counts_again(project_root, conn, chat):
    lapse(conn)
    manager.blocks_spawn(conn, JOB)
    process = coordinator(conn)
    conn.execute("UPDATE processes SET status='FINISHED',pid=NULL WHERE id=?", (process,))
    manager.heartbeat(conn, JOB, chat)
    assert manager.fresh(manager.lease(conn, JOB)) and manager_state.pending(conn, JOB)
    assert manager.lease(conn, JOB)["outage_recorded"] == 0
    assert manager.planner_authority(project_root, JOB)["holder"] == "vscode-chat"


def test_a_planner_call_by_the_attached_chat_renews_its_lease(project_root, conn, chat):
    stale = (datetime.now(UTC) - timedelta(minutes=50)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (stale, JOB))
    manager.planner_authority(project_root, JOB)
    assert manager.lease(conn, JOB)["heartbeat_at"] > stale


def test_the_chat_turn_hook_renews_only_its_own_fresh_lease(project_root, conn, chat):
    stale = (datetime.now(UTC) - timedelta(minutes=50)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (stale, JOB))
    hooks_cli.handle_stop({"cwd": str(project_root), "session_id": "another-session"})
    assert manager.lease(conn, JOB)["heartbeat_at"] == stale
    hooks_cli.handle_stop({"cwd": str(project_root), "session_id": SESSION})
    assert manager.lease(conn, JOB)["heartbeat_at"] > stale
    lapse(conn)
    lapsed = manager.lease(conn, JOB)["heartbeat_at"]
    hooks_cli.handle_pre_compact({"cwd": str(project_root), "session_id": SESSION})
    assert manager.lease(conn, JOB)["heartbeat_at"] == lapsed  # never revived without an audit


def test_a_supervised_session_never_renews_the_chat_lease(project_root, conn, chat, monkeypatch):
    stale = (datetime.now(UTC) - timedelta(minutes=50)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (stale, JOB))
    monkeypatch.setenv("AGENTKIT_PROCESS", "7")
    assert manager_presence.renew(project_root, SESSION) == []
    assert manager.lease(conn, JOB)["heartbeat_at"] == stale


def _tick_launches(project_root, conn, monkeypatch):
    runtime = conn.execute("SELECT revision FROM jobs WHERE id=?", (JOB,)).fetchone()
    conn.execute("UPDATE jobs SET planned_revision=? WHERE id=?", (runtime["revision"], JOB))
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    launched = []
    monkeypatch.setattr(supervisor, "_control_launch",
                        lambda conn, project, job, purpose, task=None: launched.append(purpose))
    supervisor.tick(project_root)
    return launched


def test_a_review_left_to_the_absent_manager_wakes_the_coordinator(project_root, conn, chat, monkeypatch):
    task = db.create_task(conn, title="builder change", status="REVIEW")
    db.update_task(conn, task, job_id=JOB)
    assert _tick_launches(project_root, conn, monkeypatch) == []  # the attached chat reviews it
    lapse(conn)
    manager.blocks_spawn(conn, JOB)
    conn.execute("UPDATE manager_state SET acknowledged_epoch=epoch WHERE job_id=?", (JOB,))
    conn.execute("UPDATE jobs SET next_check=NULL WHERE id=?", (JOB,))
    assert _tick_launches(project_root, conn, monkeypatch) == ["coordinator"]


def test_the_coordinator_prompt_names_the_waiting_reviews(project_root, conn, project, chat):
    from agentkit import external_review
    task = db.create_task(conn, title="builder change", status="REVIEW")
    db.update_task(conn, task, job_id=JOB)
    project.raw["review_mode"] = "external-manager"
    note = external_review.standin_note(conn, project, JOB)
    assert f"Tasks {task} wait in REVIEW" in note and "review_submit" in note
    project.raw["review_mode"] = "ai"
    assert external_review.standin_note(conn, project, JOB) == ""


def test_the_coordinator_always_learns_it_can_read_new_view_messages(project_root, chat):
    assert "manager_messages" in manager_chat.prompt_section(project_root, JOB)


def test_the_view_says_who_manages_the_job(project_root, conn, chat):
    assert "VS Code manager chat holds this job" in manager_presence.describe(project_root, JOB)
    lapse(conn)
    assert "starts by itself" in manager_presence.describe(project_root, JOB)
    process = coordinator(conn)
    assert f"process {process}" in manager_presence.describe(project_root, JOB)
