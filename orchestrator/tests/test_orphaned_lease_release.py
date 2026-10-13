"""F28: an expired lease of a task that is not running is released by reconcile.

AgentKit's own task 5 sat READY with a lease on workflow.py that expired on
2026-10-02 and was never released; it blocked every operator and worker claim
on that file eight days later. Missed-heartbeat expiry covers running tasks only,
and BLOCKED tasks keep their leases on purpose while paused. A lease that has
already expired, held by a task that is not running and has no live process,
protects nothing, so reconcile releases it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, reconcile

PAST = (datetime.now(UTC) - timedelta(days=8)).isoformat()
FUTURE = (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _leased(conn, status, expires_at, path="pkg/workflow.py"):
    task_id = db.create_task(conn, title=f"task {status}", status=status, owned_paths=[path])
    conn.execute("INSERT INTO leases(task_id,generation,path_glob,mode,acquired_at,expires_at) "
                 "VALUES(?,1,?,'exclusive-write',?,?)", (task_id, path, PAST, expires_at))
    return task_id


def _held(conn, task_id):
    return conn.execute("SELECT COUNT(*) FROM leases WHERE task_id=? AND released_at IS NULL",
                        (task_id,)).fetchone()[0]


@pytest.mark.parametrize("status", ["READY", "PLANNED", "DONE", "CANCELLED"])
def test_an_expired_lease_of_a_task_that_is_not_running_is_released(project_root, conn, status):
    task_id = _leased(conn, status, PAST)
    reconcile.reconcile(project_root)
    assert _held(conn, task_id) == 0
    assert db.recent_events(conn, task_id, kind="leases_released")


def test_a_lease_that_has_not_expired_stays(project_root, conn):
    task_id = _leased(conn, "READY", FUTURE)
    reconcile.reconcile(project_root)
    assert _held(conn, task_id) == 1


def test_a_paused_blocked_task_keeps_its_lease(project_root, conn):
    task_id = _leased(conn, "BLOCKED", PAST)
    reconcile.reconcile(project_root)
    assert _held(conn, task_id) == 1


def test_a_task_with_a_live_process_keeps_its_lease(project_root, conn):
    task_id = _leased(conn, "READY", PAST)
    conn.execute("INSERT INTO processes(purpose,provider,task_id,status,launch_json,started_at) "
                 "VALUES('worker','claude-code',?,'RUNNING','{}',?)", (task_id, db.utcnow()))
    reconcile.reconcile(project_root)
    assert _held(conn, task_id) == 1
