"""User provider holds survive health events and require an explicit release."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentkit import adapters, jobs, manager_state, providers, scheduler, supervisor


def test_hold_never_expires_and_blocks_scheduler(conn):
    state = providers.hold(conn, "claude-code", reason="pause subscription")
    future = datetime.now(UTC) + timedelta(days=30)
    assert not state.available_at(future)
    assert not state.due(future)
    assert providers.next_retry(conn, future) is None
    assert "held by the user" in scheduler.unavailable_adapters(conn)["claude-code"]


def test_provider_evidence_and_cooldown_do_not_release_user_hold(conn):
    providers.hold(conn, "claude-code", reason="manual pause")
    providers.observe(conn, "claude-code", {
        "available": True, "complete": True,
        "windows": [{"window": "weekly", "used_percent": 10}],
    })
    providers.begin_cooldown(conn, "claude-code", reason="usage limit")
    state = providers.get_state(conn, "claude-code")
    assert state.status == providers.HELD
    assert state.reason == "manual pause"
    assert conn.execute("SELECT used_percent FROM quota_windows").fetchone()[0] == 10
    assert providers.clear_cooldown(conn, "claude-code").available_at()


def test_hold_is_account_scoped(conn):
    providers.hold(conn, "claude-code", reason="one account", account="work")
    assert providers.get_state(conn, "claude-code", "work").status == providers.HELD
    assert providers.get_state(conn, "claude-code").available_at()
    assert providers.get_state(conn, "codex", "work").available_at()


def test_supervisor_does_not_probe_held_accounts(conn, monkeypatch):
    providers.hold(conn, "claude-code", reason="no automated inference")
    calls = []
    adapter = SimpleNamespace(account_id=lambda: "default",
                              check_availability=lambda: calls.append(True))
    monkeypatch.setattr(adapters, "get", lambda *_: adapter)
    supervisor.refresh_accounts(conn, SimpleNamespace(raw={"availability_poll_seconds": 0}),
                                ["claude-code"])
    assert calls == []
    assert providers.get_state(conn, "claude-code").status == providers.HELD


def test_hold_and_explicit_release_do_not_open_manager_outage(project_root, conn):
    jobs.create(project_root, "held-manager", "generic work", coordinator="claude-code")
    providers.hold(conn, "claude-code", reason="user decision")
    manager_state.capture_current(conn)
    providers.observe(conn, "claude-code", {"available": True, "complete": True, "windows": []})
    providers.clear_cooldown(conn, "claude-code")
    assert manager_state.state(conn, "held-manager")["epoch"] == 0
