"""Local provider health must not inherit subscription quota windows."""
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import adapters, db, providers, quota, quota_report


def test_local_health_backoff_has_no_five_hour_or_weekly_clock(conn):
    state = providers.begin_cooldown(conn, "local-opencode", reason="GPU busy", retry_at=datetime.now(UTC) + timedelta(days=7))
    assert state.status == providers.DEGRADED
    assert 0 < state.seconds_remaining() <= 60
    assert "resets" not in state.describe()
    for _ in range(8):
        state = providers.begin_cooldown(conn, "local-opencode", reason="GPU busy")
    assert state.seconds_remaining() <= 60


def test_unmetered_observation_discards_bogus_subscription_windows(conn):
    snapshot = {"available": True, "windows": [{"window": "five_hour", "used_percent": 100}], "reason": "local endpoint healthy"}
    state = providers.observe(conn, "local-opencode", snapshot)
    assert state.status == providers.AVAILABLE
    assert not conn.execute("SELECT * FROM quota_windows WHERE account_key=?", (state.key,)).fetchall()


def test_local_quota_pause_is_refused_and_does_not_change_task(conn):
    task = db.create_task(conn, title="local future task", status="RUNNING")
    with pytest.raises(ValueError, match="health recovery"):
        quota.pause(conn, task, provider="local-opencode", retry_at=None)
    assert db.get_task(conn, task)["status"] == "RUNNING"


def test_model_name_does_not_grant_unmetered_policy(monkeypatch):
    monkeypatch.setattr(adapters, "get", lambda _: object())
    assert not providers.unmetered("cloud-qwen")


def test_local_view_has_health_instead_of_allowance(conn):
    state = providers.begin_cooldown(conn, "local-opencode", reason="router stopped")
    [row] = quota_report.reports({"providers": [{"account_key": state.key, **state.to_dict()}], "quota_windows": []})
    assert row["source"] == "unmetered_health" and row["window"] == "not_applicable"
    assert row["remaining_percent"] is None
