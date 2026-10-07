"""No native IPC, provider probes, real sleep, or model turns in wake tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentkit import native_wake
from agentkit.native_ipc import overview


class Clock:
    def __init__(self):
        self.current = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def state(runtime="idle", status="completed", turn="authorizing", **extra):
    return {"threadRuntimeStatus": {"type": runtime}, "requests": [],
            "unconfirmedTurnSubmissions": [], "turns": [{"turnId": turn, "status": status}], **extra}


def healthy(percent=78):
    return {"complete": True, "available": percent < 100,
            "windows": [{"used_percent": percent, "resets_at": 1790938720, "window": "300"}]}


class Native:
    def __init__(self, clock):
        self.clock = clock
        self.snapshots = 0
        self.owner_calls = 0
        self.peer_calls = 0
        self.deliveries = []
        self.closed = False
        self.after_arm = lambda: state()
        self.changed_owner = False
        self.changed_peer = False
        self.fail_delivery = False
        self.result_path: Path | None = None

    def initialize(self):
        pass

    def current_peer(self):
        self.peer_calls += 1
        created = 2 if self.changed_peer and self.peer_calls > 1 else 1
        return {"pid": 123, "executable": "Code.exe", "created_filetime": created}

    def discover_owner(self, thread):
        self.owner_calls += 1
        return "new-owner" if self.changed_owner and self.owner_calls > 1 else "owner"

    def snapshot(self, owner, thread):
        self.snapshots += 1
        if self.snapshots == 1:
            return state("active", "inProgress", text="DO_NOT_PERSIST_PRIVATE_TEXT")
        if self.deliveries:
            return state(turn="delivered")
        return self.after_arm()

    def request(self, method, version, params, owner):
        assert method == "thread-follower-start-turn" and version == 2 and owner == "owner"
        assert self.result_path is not None
        claim = next(self.result_path.parent.glob("*.claimed.json"))
        assert json.loads(claim.read_text())["attempts"] == 1
        self.deliveries.append((self.clock.now(), params))
        if self.fail_delivery:
            raise TimeoutError("outcome unknown")
        return {"resultType": "success", "method": method, "handledByClientId": owner,
                "result": {"result": {"turn": {"id": "delivered"}}}}

    def following(self, owner, thread, enabled):
        assert enabled is False

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key in ("AGENTKIT_PROCESS", "AGENTKIT_TASK", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    clock = Clock()
    native = Native(clock)
    key = native_wake.hashlib.sha256(b"native-thread").hexdigest()[:24]
    native.result_path = tmp_path / ".ai/runtime/native-wake" / f"{key}.result.json"
    calls = []

    def availability():
        calls.append(clock.now())
        return healthy()

    def run(**kwargs):
        return native_wake.run(tmp_path, "native-thread", clock.current + timedelta(seconds=600),
                               clock.current + timedelta(seconds=900), 300,
                               connect=lambda: native, availability=kwargs.pop("availability", availability),
                               now=clock.now, pause=clock.sleep, **kwargs)
    return clock, native, calls, run


def test_never_delivers_early_and_preserves_native_input_fields(setup):
    clock, native, calls, run = setup
    earliest = clock.current + timedelta(seconds=600)
    result = run()
    assert len(native.deliveries) == 1 and native.deliveries[0][0] >= earliest
    request = native.deliveries[0][1]["turnStart"]["request"]
    assert request["input"][0]["text_elements"] == []
    assert "textElements" not in request["input"][0]
    assert request["threadId"] == "native-thread"
    assert native.deliveries[0][1]["turnStart"]["context"] == {"inheritThreadSettings": True}
    assert calls[0] < earliest and any(stamp < earliest for stamp in calls[1:])
    assert result["native_turn_completed"] and result["wake_kind"] == "idle-wake-only"
    assert not result["actual_exhaustion_observed"] and not result["provider_confirmed_recovery"]
    assert "No 100% exhaustion" in request["input"][0]["text"]
    assert "manager-checkpoint.json" in request["input"][0]["text"]
    assert "DO_NOT_PERSIST_PRIVATE_TEXT" not in native.result_path.read_text()
    assert native.closed


def test_observes_actual_exhaustion_between_baseline_and_scheduled_time(setup):
    clock, native, _, run = setup
    start = clock.current
    result = run(availability=lambda: healthy(100 if 300 <= (clock.current - start).total_seconds() < 600 else 78))
    assert result["baseline_quota"]["exhausted"] is False
    assert result["actual_exhaustion_observed"] and result["provider_confirmed_recovery"]
    assert result["wake_kind"] == "quota-recovery" and len(native.deliveries) == 1


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_authorizing_terminal_turn_may_wake_without_interrupt(setup, status):
    _, native, _, run = setup
    native.after_arm = lambda: state(status=status)
    assert run()["native_turn_completed"] and len(native.deliveries) == 1


@pytest.mark.parametrize("blocked", ["active", "requests", "unconfirmed", "pending"])
def test_active_turn_approval_or_input_prevents_delivery(setup, blocked):
    _, native, _, run = setup
    if blocked == "active":
        native.after_arm = lambda: state("active", "inProgress")
    elif blocked == "requests":
        native.after_arm = lambda: state(requests=[{"approval": True}])
    elif blocked == "unconfirmed":
        native.after_arm = lambda: state(unconfirmedTurnSubmissions=[{"input": True}])
    else:
        native.after_arm = lambda: state(pendingUserInput=True)
    assert run()["status"] == "deadline_reached_no_delivery"
    assert not native.deliveries


@pytest.mark.parametrize("change", ["turn", "owner", "peer", "pipe"])
def test_changed_session_or_restart_cancels_without_delivery(setup, change):
    _, native, _, run = setup
    if change == "turn":
        native.after_arm = lambda: state(turn="intervening-user-turn")
    elif change == "owner":
        native.changed_owner = True
    elif change == "peer":
        native.changed_peer = True
    else:
        def disconnected():
            raise EOFError("VS Code stopped")
        native.after_arm = disconnected
    assert run()["status"] == "cancelled_no_delivery"
    assert not native.deliveries


def test_persistent_arm_and_claim_prevent_restart_and_unknown_delivery_retry(setup):
    _, native, calls, run = setup
    native.fail_delivery = True
    first = run()
    assert first["status"] == "delivery_outcome_unknown_no_retry"
    assert len(native.deliveries) == 1 and not first["native_turn_completed"]
    quota_calls = len(calls)
    assert run()["status"] == "already_armed_no_retry"
    assert len(native.deliveries) == 1 and len(calls) == quota_calls


def test_unknown_provider_metadata_never_becomes_clock_only_permission(setup):
    _, native, _, run = setup
    result = run(availability=lambda: {"available": None, "complete": False, "windows": []})
    assert result["status"] == "deadline_reached_no_delivery" and not native.deliveries
    assert not result["provider_confirmed_recovery"]


def test_expired_deadline_and_timezone_errors_prevent_connection(setup, tmp_path):
    clock, native, _, _ = setup
    with pytest.raises(ValueError):
        native_wake.run(tmp_path, "native-thread", clock.current, clock.current,
                        connect=lambda: pytest.fail("opened IPC"), now=clock.now)
    with pytest.raises(ValueError, match="timezone"):
        native_wake.timestamp("2026-10-02T12:58:40")
    assert not native.deliveries


def test_unknown_native_request_schema_fails_closed():
    with pytest.raises(RuntimeError):
        overview({"turns": []})
    with pytest.raises(RuntimeError):
        overview(state(unconfirmedTurnSubmissions={"unknown": True}))


def test_history_pagination_keeps_latest_turn_authority(setup):
    _, native, _, run = setup
    native.after_arm = lambda: state(turns=[{
        "turnId": "older-history", "status": "completed"}, {
        "turnId": "authorizing", "status": "completed"}])
    result = run()
    assert result["native_turn_completed"] and len(native.deliveries) == 1
    assert result["ui_health_verified"] is False
    assert result["last_quota_observed_at"]


@pytest.mark.parametrize("turn_id", [None, "", " ", 1, True, {}, []])
def test_malformed_latest_turn_identity_fails_closed(turn_id):
    with pytest.raises(RuntimeError, match="identity unknown"):
        overview(state(turn=turn_id))


def quota_mode_native(native):
    native.result_path = native.result_path.parent.parent / "native-quota-wake" / native.result_path.name


def test_quota_only_never_delivers_without_observed_exhaustion(setup):
    _, native, _, run = setup
    quota_mode_native(native)
    result = run(quota_recovery_only=True)
    assert result["status"] == "deadline_reached_no_delivery"
    assert not result["actual_exhaustion_observed"]
    assert result["turn_start_attempts"] == 0 and not native.deliveries


def test_quota_only_requires_recovery_and_does_not_reuse_idle_diagnostic_claim(setup):
    _, native, _, run = setup
    idle_dir = native.result_path.parent
    idle_dir.mkdir(parents=True)
    idle_dir.joinpath(native.result_path.name.replace(".result.json", ".claimed.json")).write_text("{}")
    quota_mode_native(native)
    observations = iter([healthy(100), healthy(100), healthy(3)])
    result = run(quota_recovery_only=True, availability=lambda: next(observations, healthy(3)))
    assert result["actual_exhaustion_observed"] and result["provider_confirmed_recovery"]
    assert result["wake_kind"] == "quota-recovery"
    assert result["native_turn_completed"] and len(native.deliveries) == 1


def test_quota_only_rechecks_account_before_consuming_delivery_claim(setup):
    _, native, _, run = setup
    quota_mode_native(native)
    observations = iter([healthy(100), healthy(100), healthy(100), healthy(3), healthy(100)])
    result = run(quota_recovery_only=True, availability=lambda: next(observations, healthy(100)))
    assert result["actual_exhaustion_observed"]
    assert result["turn_start_attempts"] == 0 and not native.deliveries
    assert not list(native.result_path.parent.glob("*.claimed.json"))


def test_wake_prompt_is_project_neutral_and_obeys_latest_pause(tmp_path):
    from agentkit.native_wake import resume_prompt
    prompt = resume_prompt(tmp_path / ".ai/runtime/native-wake/result.json", False)
    assert "AgentKit/pilot readiness" not in prompt and "Claude handles backend" not in prompt and "three AI sessions" not in prompt
    assert "latest user-authorized" in prompt and "execution is paused" in prompt
    assert "submission acceptance alone" in prompt


def test_operator_cancellation_prevents_even_initial_connection(setup, tmp_path):
    from agentkit.native_wake_control import cancel
    _, native, _, run = setup
    cancel(tmp_path, "native-thread", "User stopped automatic work")
    result = run()
    assert result["turn_start_attempts"] == 0
    assert not native.deliveries and native.snapshots == 0


def test_cancel_between_idle_checks_does_not_submit(setup, tmp_path):
    from agentkit.native_wake_control import cancel
    _, native, _, run = setup
    original = native.after_arm
    def cancelled():
        cancel(tmp_path, "native-thread", "Stop reading-time wake")
        return original()
    native.after_arm = cancelled
    result = run()
    assert result["turn_start_attempts"] == 0 and not native.deliveries
    assert result["status"] == "cancelled_no_delivery"


@pytest.mark.parametrize("recovery,pending,expected", [(True, False, 1), (False, False, 0), (True, True, 0)])
def test_quota_system_error_needs_actual_reset_and_no_pending_input(setup, recovery, pending, expected):
    clock, native, _, run = setup
    start = clock.current
    native.after_arm = lambda: state("systemError", "failed", pendingUserInput=pending)
    key = native_wake.hashlib.sha256(b"native-thread").hexdigest()[:24]
    native.result_path = native.result_path.parents[1] / "native-quota-wake" / f"{key}.result.json"
    def available():
        return healthy(100 if recovery and 300 <= (clock.current-start).total_seconds() < 600 else 20)
    result = run(quota_recovery_only=True, availability=available)
    assert len(native.deliveries) == expected
    assert result["turn_start_attempts"] == expected


def test_idle_only_diagnostic_cannot_wake_a_system_error(setup):
    _, native, _, run = setup
    native.after_arm = lambda: state("systemError", "failed")
    assert run()["turn_start_attempts"] == 0
    assert not native.deliveries


@pytest.mark.parametrize("exhausted", [False, True])
def test_reset_timed_diagnostic_requires_proven_recovery_for_system_error(setup, exhausted):
    clock, native, _, run = setup
    native.after_arm = lambda: state("systemError", "failed")
    start = clock.current
    result = run(availability=lambda: healthy(
        100 if exhausted and (clock.current - start).total_seconds() < 600 else 3))
    assert result["native_turn_completed"] is exhausted
    assert len(native.deliveries) == int(exhausted)
    assert result["actual_exhaustion_observed"] is exhausted


def test_started_turn_receipt_is_persisted_before_completion(setup, monkeypatch):
    clock, native, _, run = setup
    original_snapshot = native.snapshot
    original_sleep = clock.sleep
    observations = []

    def snapshot(owner, thread):
        if native.deliveries and not observations:
            return state("active", "inProgress", turn="delivered")
        return original_snapshot(owner, thread)

    def sleep(seconds):
        if native.deliveries:
            saved = json.loads(native.result_path.read_text())
            observations.append(saved)
            assert saved["native_turn_started"] is True
            assert saved["status"] == "native_turn_started_completion_unverified"
            assert not saved["native_turn_completed"]
            from agentkit import db, recovery_store
            conn = db.connect(native.result_path.parents[3])
            try:
                intent = recovery_store.get(conn, saved["recovery_intent_id"])
                assert intent["state"] == "TURN_STARTED"
            finally:
                conn.close()
        original_sleep(seconds)

    monkeypatch.setattr(native, "snapshot", snapshot)
    monkeypatch.setattr(clock, "sleep", sleep)
    result = run()
    assert observations and result["native_turn_completed"]
    assert len(native.deliveries) == 1
