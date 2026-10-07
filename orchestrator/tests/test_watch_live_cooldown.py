"""Unavailable unused providers must not delay the active worker path."""
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, providers, watch


@pytest.mark.parametrize("status", ["RUNNING", "VERIFYING", "REVIEW", "INTEGRATION_READY", "INTEGRATING"])
def test_unrelated_cooldown_preserves_live_poll_interval(conn, make_task, status):
    task = make_task("bounded work", ["src/work.py"])
    db.update_task(conn, task, status=status)
    providers.begin_cooldown(conn, "local-opencode", reason="unused router stopped", retry_at=datetime.now(UTC) + timedelta(hours=1))
    assert watch.next_sleep(conn, 20, launched=False) == 20


def test_quota_only_wait_remains_patient(conn):
    providers.begin_cooldown(conn, "claude-code", reason="quota", retry_at=datetime.now(UTC) + timedelta(hours=1))
    assert watch.next_sleep(conn, 20, launched=False) == watch.MAX_SLEEP_SECONDS
