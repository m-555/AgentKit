"""F26: a task waiting out a usage limit is not prepared again on every pass.

Task 142, parked by the limit, was refused by its recovery intent only at the
very end of each launch, after the claim had bumped its generation and its
files, guards and environment had been prepared. Every 20-second pass left a
new generation, checkpoint and gate row (generation 46 by morning), and the
churn changed the manager audit digest between audit and acknowledgement, so a
woken coordinator could never clear the recovery barrier (F23). The launch now
checks the recovery intent first, with no side effects.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from agentkit import db, models, providers, recovery_runtime
from agentkit import recovery_store as store
from tests.test_empty_attempt_fresh_start import _launch, stopped_empty  # noqa: F401 - fixture

OPUS = models.Profile("opus", "claude-code", "claude-opus-5-5", "xhigh", 2)


def _park(conn, task_id):
    session = store.register(conn, identifier=f"queued:{task_id}:1", provider="claude-code", account="default",
                             host="cli", role="worker", reference=f"task:{task_id}:1", task_id=task_id)
    store.arm(conn, session, json.dumps({"generation": 1}), {"generation": 1}, "quota")


def _evidence(conn, age):
    providers.observe(conn, "claude-code", {"available": True, "complete": True, "windows": []})
    conn.execute("UPDATE provider_state SET detected_at=? WHERE account_key=?",
                 ((datetime.now(UTC) - age).isoformat(), providers.account_key("claude-code")))


def _churn(conn, task_id):
    task = db.get_task(conn, task_id)
    checkpoints = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()[0]
    prepared = len(db.recent_events(conn, task_id, kind="worker_files_prepared", limit=50))
    return task["generation"], task["status"], checkpoints, prepared


def test_a_waiting_recovery_refuses_before_any_preparation(project_root, conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    _park(conn, task_id)
    _evidence(conn, timedelta(hours=7))
    before = _churn(conn, task_id)
    for _ in range(3):  # three supervisor passes
        ok, detail = _launch(conn, project_root, stopped_empty, OPUS)
        assert not ok and "waiting for fresh" in detail
    assert _churn(conn, task_id) == before
    assert not stopped_empty["launched"]


def test_fresh_availability_lets_the_guarded_launch_proceed(project_root, conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    _park(conn, task_id)
    _evidence(conn, timedelta(seconds=10))
    ok, detail = _launch(conn, project_root, stopped_empty, OPUS)
    assert "waiting for fresh" not in str(detail)


def test_a_parked_turn_that_cannot_resume_is_cancelled_once_and_the_task_launches_fresh(
        project_root, conn, stopped_empty):  # noqa: F811
    """F27: the cancellation used to roll back with the refused launch, so it repeated forever.

    Task 142's saved proof said generation 1 while churn had pushed it to 77; every
    pass cancelled the intent, raised, and rolled the cancellation back.
    """
    task_id = stopped_empty["task"]
    _park(conn, task_id)
    db.update_task(conn, task_id, generation=5)  # preserved state no longer matches the parked turn
    _evidence(conn, timedelta(seconds=10))
    with db.immediate_transaction(conn):  # as processes.start runs it
        claim = recovery_runtime.before_launch(conn, project_root, purpose="worker", provider="claude-code",
                                               task_id=task_id)
    assert claim is None  # launch fresh under the normal guards
    assert conn.execute("SELECT state FROM recovery_intents").fetchone()[0] == "CANCELLED"
    assert recovery_runtime.pending(conn, task_id=task_id) is None
