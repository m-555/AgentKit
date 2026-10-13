"""F18: handing over a lease that already lapsed still opens a recovery epoch.

A heartbeat on an expired lease records the outage, so the next manager must
audit and acknowledge what happened while nobody held the job. Handover used to
rebind an expired lease silently: on 2026-10-09 a lease three days stale was
taken over with no audit. Only a lease that never lapsed keeps its epoch.
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
    job = jobs.create(project_root, "handover-expired", "Keep managing across chat sessions", "claude-code")
    job = jobs.pin_coordinator(project_root, job["id"], OPUS)
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=revision WHERE id=?", (job["id"],))
    old = _bridge()
    token = manager.attach(conn, project_root, job["id"], "first-chat", old.pid, ttl_seconds=600,
                           session_ref="first-session")
    new = _bridge()
    yield job, token, new
    for process in (old, new):
        process.kill()
        process.wait()


def _lapse(conn, job_id):
    conn.execute("UPDATE manager_leases SET heartbeat_at='2026-10-06T20:13:00+00:00' WHERE job_id=?", (job_id,))


def test_handing_over_an_expired_lease_opens_a_recovery_epoch(conn, attached):
    job, token, new = attached
    epoch = manager_state.state(conn, job["id"])["epoch"]
    _lapse(conn, job["id"])
    fresh_token = manager.handover(conn, job["id"], token, "second-chat", new.pid,
                                   session_ref="second-session", ttl_seconds=600)
    assert manager_state.state(conn, job["id"])["epoch"] == epoch + 1
    assert manager_state.pending(conn, job["id"])
    assert manager.require_lease(conn, job["id"], fresh_token)["holder"] == "second-chat"
    assert "handover" in manager_state.state(conn, job["id"])["outage_reason"]


def test_handover_after_an_outage_matches_a_late_heartbeat(conn, attached):
    job, token, new = attached
    _lapse(conn, job["id"])
    manager.heartbeat(conn, job["id"], token)  # the outage is now recorded once
    epoch = manager_state.state(conn, job["id"])["epoch"]
    manager.handover(conn, job["id"], token, "second-chat", new.pid, session_ref="second-session",
                     ttl_seconds=600)
    assert manager_state.state(conn, job["id"])["epoch"] == epoch  # the renewed lease never lapsed again


def test_a_live_lease_handover_keeps_its_epoch(conn, attached):
    job, token, new = attached
    epoch = manager_state.state(conn, job["id"])["epoch"]
    manager.handover(conn, job["id"], token, "second-chat", new.pid, session_ref="second-session",
                     ttl_seconds=600)
    assert manager_state.state(conn, job["id"])["epoch"] == epoch
    assert not manager_state.pending(conn, job["id"])
    event = db.recent_events(conn, kind="external_manager_handover", limit=1)[0]
    assert event["detail"]["lapsed"] is False
