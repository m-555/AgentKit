"""F25: a task parked by a usage limit resumes after the reset, however late it is noticed.

On 2026-10-09 a Claude worker hit the five-hour limit at 20:23 UTC. Claude was
available again at 00:30, but task 142 was still refused at 08:00 because:
- the recorded quota windows still said 100% used, with reset times in the past;
- readiness needs availability evidence under 300 s old, and the supervisor never
  re-checks an AVAILABLE Claude account (to save tokens), so once the first
  five minutes after the reset were lost the evidence could never be fresh.
Readiness now ignores windows that have already reset, and a waiting recovery
intent earns one rate-limited re-check of an AVAILABLE account.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentkit import adapters, providers, recovery_runtime, supervisor
from agentkit import recovery_store as store

NOW = datetime.now(UTC)


def _available_since(conn, hours, provider="claude-code"):
    providers.observe(conn, provider, {"available": True, "complete": True, "windows": []})
    conn.execute("UPDATE provider_state SET detected_at=? WHERE account_key=?",
                 ((NOW - timedelta(hours=hours)).isoformat(), providers.account_key(provider)))


def _window(conn, name, used, resets_at, provider="claude-code"):
    conn.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)",
                 (providers.account_key(provider), "claude", name, used, resets_at.isoformat(),
                  (NOW - timedelta(hours=8)).isoformat(), "worker quota event"))


def _waiting(conn, provider="claude-code"):
    session = store.register(conn, identifier="cli:4242", provider=provider, account="default", host="cli",
                             role="worker", reference="task:7:1", task_id=7)
    return store.arm(conn, session, json.dumps({"generation": 1}), {"generation": 1}, "quota")


def _claude(calls):
    def check():
        calls.append(1)
        return {"available": True, "complete": True, "windows": []}
    return SimpleNamespace(account_id=lambda: "default", availability_requires_inference=True, check_availability=check)


def test_windows_that_already_reset_do_not_hold_recovery_back(conn):
    _available_since(conn, 0)
    _window(conn, "five_hour", 100.0, NOW - timedelta(hours=7))
    _window(conn, "seven_day", 100.0, NOW - timedelta(days=5))
    _window(conn, "monthly", 40.0, NOW + timedelta(days=9))
    windows = recovery_runtime.account_evidence(conn, "claude-code", "default")["windows"]
    assert [w["window"] for w in windows] == ["monthly"]


def test_a_window_still_running_at_100_percent_is_kept(conn):
    _available_since(conn, 0)
    _window(conn, "five_hour", 100.0, NOW + timedelta(hours=1))
    assert recovery_runtime.account_evidence(conn, "claude-code", "default")["windows"][0]["used_percent"] == 100.0


def test_a_waiting_intent_earns_one_recheck_of_a_stale_available_claude_account(conn, project, monkeypatch):
    _available_since(conn, 7.5)
    _waiting(conn)
    calls = []
    monkeypatch.setattr(adapters, "get", lambda *a: _claude(calls))
    supervisor.refresh_accounts(conn, project, ["claude-code"])
    assert len(calls) == 1
    observed = providers.get_state(conn, "claude-code", "default").detected_at
    assert datetime.fromisoformat(observed) > NOW - timedelta(minutes=1)
    supervisor.refresh_accounts(conn, project, ["claude-code"])  # fresh now: no second ping
    assert len(calls) == 1


def test_without_a_waiting_intent_an_available_claude_account_costs_nothing(conn, project, monkeypatch):
    _available_since(conn, 7.5)
    monkeypatch.setattr(adapters, "get", lambda *a: _claude([]) if False else SimpleNamespace(
        account_id=lambda: "default", availability_requires_inference=True,
        check_availability=lambda: pytest.fail("paid ping with nothing waiting")))
    supervisor.refresh_accounts(conn, project, ["claude-code"])


def test_a_waiting_intent_for_another_provider_does_not_ping_claude(conn, project, monkeypatch):
    _available_since(conn, 7.5)
    _waiting(conn, provider="codex")
    monkeypatch.setattr(adapters, "get", lambda name, *a: SimpleNamespace(
        account_id=lambda: "default", availability_requires_inference=True,
        check_availability=lambda: pytest.fail(f"pinged {name}")))
    supervisor.refresh_accounts(conn, project, ["claude-code"])
