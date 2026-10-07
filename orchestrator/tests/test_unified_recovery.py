"""No inference: shared policy, restart/duplicate delivery and real CLI handoff barriers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, providers, recovery_runtime, wake_adapters
from agentkit import recovery_store as store

NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)


def intent(conn, *, role="worker", host="cli", policy="subscription", provider="codex"):
    identifier = store.register(conn, identifier="session", provider=provider, account="default",
                                host=host, role=role, reference="reference", policy=policy)
    return store.arm(conn, identifier, "revision-1", {"generation": 1, "head": "saved"}, "quota")


def available(**extra):
    return {"observed_at": NOW.isoformat(), "available": True, "complete": True,
            "healthy": True, "windows": [{"used_percent": 42, "window": "300"}], **extra}


def ready(conn, row, **extra):
    options = dict(authorized=True, old_owner_stopped=True, proof=row["proof"], now=NOW)
    options.update(extra)
    return wake_adapters.ready(conn, row["id"], options.pop("evidence", available()), **options)


@pytest.mark.parametrize("role", ["worker", "manager"])
@pytest.mark.parametrize("host", ["cli", "codex-vscode"])
@pytest.mark.parametrize("policy", ["subscription", "unmetered"])
def test_common_policy_for_roles_hosts_accounts(conn, role, host, policy):
    row = intent(conn, role=role, host=host, policy=policy)
    assert ready(conn, row)
    attempt = store.claim(conn, row["id"], "watcher", proof=row["proof"], authorization="revision-1", now=NOW)
    assert attempt
    assert not store.claim(conn, row["id"], "competing", proof=row["proof"], authorization="revision-1", now=NOW)


def test_weekly_still_blocks_after_five_hour_reset(conn):
    row = intent(conn)
    evidence = available(windows=[{"used_percent": 0, "window": "300"},
                                  {"used_percent": 100, "window": "10080"}])
    assert not ready(conn, row, evidence=evidence)
    assert store.get(conn, row["id"])["state"] == "WAITING_AVAILABILITY"


def test_local_ignores_subscription_windows_but_requires_health(conn):
    row = intent(conn, policy="unmetered", provider="local-opencode")
    assert ready(conn, row, evidence=available(windows=[{"used_percent": 100}]))
    assert not ready(conn, row, evidence=available(healthy=False))


@pytest.mark.parametrize("override", [dict(old_owner_stopped=False), dict(host_healthy=False),
                                      dict(evidence=available(observed_at=(NOW-timedelta(minutes=6)).isoformat()))])
def test_fresh_health_and_stopped_owner_required(conn, override):
    row = intent(conn)
    assert not ready(conn, row, **override)


@pytest.mark.parametrize("override", [dict(authorized=False), dict(proof={"head": "changed"})])
def test_user_pause_or_evidence_change_cancels(conn, override):
    row = intent(conn)
    assert not ready(conn, row, **override)
    assert store.get(conn, row["id"])["state"] == "CANCELLED"


def test_manager_needs_packet_not_sleeping_ack(conn):
    row = intent(conn, role="manager")
    assert not ready(conn, row, audit_packet=False)
    assert ready(conn, row, audit_packet=True)


def test_unsupported_editor_requires_manual_action(conn):
    row = intent(conn, host="claude-editor", role="manager", provider="claude-code")
    assert not ready(conn, row)
    assert store.get(conn, row["id"])["state"] == "NEEDS_USER_ACTION"


def test_restart_does_not_rearm_claim_and_cancel_is_permanent(conn, project_root):
    row = intent(conn)
    assert ready(conn, row)
    attempt = store.claim(conn, row["id"], "watcher", proof=row["proof"], authorization="revision-1", now=NOW)
    other = db.connect(project_root)
    try:
        duplicate = store.arm(other, "session", "revision-1", row["proof"], "quota")
        assert duplicate["attempt_id"] == attempt
        assert not store.claim(other, row["id"], "new-watch", proof=row["proof"], authorization="revision-1", now=NOW)
        store.cancel(other, row["id"], "User stopped")
        assert store.arm(other, "session", "revision-1", row["proof"], "quota")["state"] == "CANCELLED"
    finally:
        other.close()


class Transport:
    def __init__(self, *, timeout=False):
        self.calls = 0
        self.timeout = timeout
        self.receipt = {}

    def deliver(self, row, attempt):
        self.calls += 1
        self.receipt = {"attempt_id": attempt}
        if self.timeout:
            raise TimeoutError("Lost reply")
        return {"accepted": True, "reason": "Submitted"}

    def observe(self, row):
        return self.receipt


@pytest.mark.parametrize("timeout", [False, True])
def test_accepted_or_lost_reply_not_success_and_reconciles_actual_turn(conn, timeout):
    row = intent(conn)
    assert ready(conn, row)
    transport = Transport(timeout=timeout)
    attempt = wake_adapters.deliver(conn, row["id"], transport, proof=row["proof"], authorization="revision-1", owner="host")
    assert store.get(conn, row["id"])["state"] == ("RECONCILING" if timeout else "DELIVERY_ACCEPTED")
    assert not wake_adapters.deliver(conn, row["id"], transport, proof=row["proof"], authorization="revision-1", owner="host")
    assert transport.calls == 1
    transport.receipt.update(started=True, completed=True, success=True)
    assert wake_adapters.observe(conn, row["id"], transport)["state"] == "RECOVERED"
    assert store.get(conn, row["id"])["attempt_id"] == attempt


def test_completion_without_observed_start_never_passes(conn):
    row = intent(conn)
    assert ready(conn, row)
    transport = Transport()
    wake_adapters.deliver(conn, row["id"], transport, proof=row["proof"], authorization="revision-1", owner="host")
    transport.receipt.update(completed=True, success=True)
    assert wake_adapters.observe(conn, row["id"], transport)["state"] == "DELIVERY_ACCEPTED"


def test_expired_claim_reconciles_instead_of_resending(conn):
    row = intent(conn)
    assert ready(conn, row)
    store.claim(conn, row["id"], "host", proof=row["proof"], authorization="revision-1", lease_seconds=30, now=NOW)
    store.reconcile_expired(conn, now=NOW+timedelta(seconds=31))
    assert store.get(conn, row["id"])["state"] == "RECONCILING"


def test_receipt_cannot_ack_other_attempt(conn):
    row = intent(conn)
    assert ready(conn, row)
    store.claim(conn, row["id"], "host", proof=row["proof"], authorization="revision-1", now=NOW)
    with pytest.raises(PermissionError):
        store.move(conn, row["id"], "DELIVERY_ACCEPTED", "Wrong attempt", attempt_id="wrong")


def test_changed_or_transferred_task_never_reclaims_primary(conn, project_root, make_task):
    identifier = make_task("Preserved", ["services/media.py"], status="BLOCKED", worktree=str(project_root), adapter="codex")
    task = db.get_task(conn, identifier)
    row = recovery_runtime.park_worker(conn, task, {"provider":"codex", "paused_at":"one"})
    providers.clear_cooldown(conn, "codex")
    (project_root/"services/media.py").write_text("CHANGED = True\n")
    assert not recovery_runtime.authorize(conn, project_root, row)[0]
    assert store.get(conn, row["id"])["state"] == "CANCELLED"


def test_session_cannot_change_provider_identity(conn):
    intent(conn)
    with pytest.raises(ValueError):
        store.register(conn, identifier="session", provider="claude-code", account="default",
                       host="cli", role="worker", reference="reference")
