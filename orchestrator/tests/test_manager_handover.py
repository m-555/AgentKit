"""Handing an external manager lease to the user's next chat session.

The user replaces a manager chat when its context grows. The new session must
own the same lease, with no moment in which a CLI coordinator could be
spawned, no new recovery epoch, and no way for the old session to keep acting.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from agentkit import db, jobs, manager, manager_state, models

OPUS = models.Profile("opus", "claude-code", "claude-opus-5-5", "high", 2)


def _bridge():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


@pytest.fixture
def attached(project_root, conn, monkeypatch):
    for name in ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(name, raising=False)
    job = jobs.create(project_root, "handover", "Keep managing across chat sessions", "claude-code")
    job = jobs.pin_coordinator(project_root, job["id"], OPUS)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    old = _bridge()
    token = manager.attach(conn, project_root, job["id"], "first-chat", old.pid, ttl_seconds=600,
                           session_ref="first-session")
    yield job, token, old
    old.kill()
    old.wait()


def test_handover_rebinds_the_lease_and_retires_the_old_credential(project_root, conn, attached):
    job, token, _old = attached
    epoch = manager_state.state(conn, job["id"])["epoch"]
    new = _bridge()
    try:
        fresh_token = manager.handover(conn, job["id"], token, "second-chat", new.pid,
                                       session_ref="second-session", ttl_seconds=600)
        record = manager.require_lease(conn, job["id"], fresh_token)
        assert (record["holder"], record["pid"], record["session_ref"]) == ("second-chat", new.pid, "second-session")
        assert (record["provider"], record["model"]) == ("claude-code", "claude-opus-5-5")
        with pytest.raises(PermissionError):
            manager.require_lease(conn, job["id"], token)
        assert manager_state.state(conn, job["id"])["epoch"] == epoch
        assert manager.blocks_spawn(conn, job["id"])
        event = db.recent_events(conn, kind="external_manager_handover", limit=1)[0]
        assert event["detail"]["from"] == "first-chat" and event["detail"]["to"] == "second-chat"
    finally:
        new.kill()
        new.wait()


def test_handover_works_after_the_old_chat_process_has_exited(project_root, conn, attached):
    job, token, old = attached
    old.kill()
    old.wait()
    new = _bridge()
    try:
        fresh_token = manager.handover(conn, job["id"], token, "second-chat", new.pid,
                                       session_ref="second-session", ttl_seconds=600)
        assert manager.require_lease(conn, job["id"], fresh_token)["pid"] == new.pid
        assert not manager_state.pending(conn, job["id"])
    finally:
        new.kill()
        new.wait()


def test_handover_needs_the_current_credential(project_root, conn, attached):
    job, _token, _old = attached
    new = _bridge()
    try:
        with pytest.raises(PermissionError):
            manager.handover(conn, job["id"], "not-the-credential", "intruder", new.pid,
                             session_ref="other", ttl_seconds=600)
        assert manager.lease(conn, job["id"])["holder"] == "first-chat"
    finally:
        new.kill()
        new.wait()


def test_handover_needs_a_live_new_bridge_and_a_session(project_root, conn, attached):
    job, token, _old = attached
    gone = _bridge()
    gone.kill()
    gone.wait()
    with pytest.raises(ValueError):
        manager.handover(conn, job["id"], token, "second-chat", gone.pid, session_ref="s", ttl_seconds=600)
    new = _bridge()
    try:
        with pytest.raises(ValueError):
            manager.handover(conn, job["id"], token, "second-chat", new.pid, session_ref=" ", ttl_seconds=600)
    finally:
        new.kill()
        new.wait()
    assert manager.lease(conn, job["id"])["holder"] == "first-chat"


def test_a_worker_or_coordinator_cannot_take_over_the_lease(project_root, conn, attached, monkeypatch):
    job, token, _old = attached
    monkeypatch.setenv("AGENTKIT_PROCESS", "9")
    new = _bridge()
    try:
        with pytest.raises(PermissionError):
            manager.handover(conn, job["id"], token, "coordinator", new.pid, session_ref="s", ttl_seconds=600)
    finally:
        new.kill()
        new.wait()
