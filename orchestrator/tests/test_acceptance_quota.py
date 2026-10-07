"""Acceptance test 25: provider usage limits, cooldown and automatic resume.

The scenario these defend:

    queue a batch of features, leave the machine, Claude's five-hour allowance
    runs out mid-task, Codex keeps working, and the paused tasks resume by
    themselves when the allowance resets.

The bug that motivated it: "Claude usage limit reached. Your limit will reset at
3pm" matched none of the old provider patterns, so it fell through to CRASH and
*consumed an attempt*. Three of those marked a perfectly healthy task
NEEDS_REPLAN. Classification is therefore tested against the real wording, not a
paraphrase.

Nothing here increases anyone's quota. Parallel workers share one account
allowance; what is being tested is that the machine keeps doing whatever work is
possible and loses nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, errors, providers, quota, scheduler, watch
from agentkit import statemachine as sm
from agentkit.adapters import get as get_adapter
from agentkit.capabilities import CapabilitySet

#: Verbatim wording from Claude Code. Paraphrasing here would defeat the point.
CLAUDE_USAGE_LIMIT = "Claude usage limit reached. Your limit will reset at 3pm."
CLAUDE_USAGE_LIMIT_WEEKLY = "Weekly limit reached. Your limit will reset at 9:30 am."
CODEX_USAGE_LIMIT = "You have hit your usage limit. Resets in 42 minutes."


def _caps(name: str, **overrides: bool) -> CapabilitySet:
    caps = CapabilitySet(adapter=name)
    for key in ("workspace_sandbox", "prewrite_file_guard", "shell_guard",
                "mcp_stdio", "structured_output", "resume_session", "event_stream"):
        caps.set(key, True)
    for key, value in overrides.items():
        caps.set(key, value)
    return caps


class TestUsageLimitClassification:
    """Not CRASH, no attempt consumed, reset time captured."""

    def test_real_claude_wording_is_a_usage_limit(self):
        result = get_adapter("claude-code").classify_error(CLAUDE_USAGE_LIMIT, 1)
        assert result.kind == errors.USAGE_LIMIT
        assert result.kind != errors.CRASH

    def test_usage_limit_does_not_consume_an_attempt(self):
        result = get_adapter("claude-code").classify_error(CLAUDE_USAGE_LIMIT, 1)
        assert not result.consumes_attempt

    def test_reset_time_is_parsed(self):
        result = get_adapter("claude-code").classify_error(CLAUDE_USAGE_LIMIT, 1)
        assert result.retry_at is not None
        assert result.retry_at.hour == 15

    def test_weekly_limit_wording(self):
        result = get_adapter("claude-code").classify_error(CLAUDE_USAGE_LIMIT_WEEKLY, 1)
        assert result.kind == errors.USAGE_LIMIT
        # A clock without a date cannot identify a weekly reset.
        assert result.retry_at is None

    def test_codex_wording(self):
        result = get_adapter("codex").classify_error(CODEX_USAGE_LIMIT, 1)
        assert result.kind == errors.USAGE_LIMIT
        assert result.retry_at is not None

    @pytest.mark.parametrize("text,expected", [
        ("Error 429: rate limit exceeded", errors.RATE_LIMIT),
        ("API Error: 503 Service Unavailable", errors.PROVIDER_OUTAGE),
        ("overloaded_error", errors.PROVIDER_OUTAGE),
        ("401 Unauthorized", errors.AUTH_ERROR),
        ("Invalid API key. Please run /login", errors.AUTH_ERROR),
        ("Traceback: ZeroDivisionError", errors.CRASH),
    ])
    def test_other_classes_stay_distinct(self, text, expected):
        assert get_adapter("claude-code").classify_error(text, 1).kind == expected

    def test_only_task_failures_consume_attempts(self):
        for text in (CLAUDE_USAGE_LIMIT, "429 rate limit", "503 overloaded", "401"):
            assert not errors.classify(text, 1).consumes_attempt
        assert errors.classify("segfault", 139).consumes_attempt

    def test_usage_limit_is_not_mistaken_for_a_rate_limit(self):
        """Both contain the word 'limit'; the responses differ by hours."""
        assert errors.classify(CLAUDE_USAGE_LIMIT, 1).kind == errors.USAGE_LIMIT
        assert errors.classify("429 too many requests", 1).kind == errors.RATE_LIMIT


class TestRetryAtParsing:
    def test_bare_12_hour_clock(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=UTC).astimezone()
        assert providers.parse_retry_at("resets at 3pm", now=now).hour == 15

    def test_12_hour_with_minutes(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=UTC).astimezone()
        parsed = providers.parse_retry_at("reset at 3:30 pm", now=now)
        assert (parsed.hour, parsed.minute) == (15, 30)

    def test_crossing_midnight_rolls_to_tomorrow(self):
        """11pm now, 'resets at 2am' means tomorrow, not 21 hours ago."""
        now = datetime(2026, 1, 5, 23, 0, tzinfo=UTC).astimezone()
        parsed = providers.parse_retry_at("your limit will reset at 2am", now=now)
        assert parsed > now
        assert parsed.day == (now.day + 1) or parsed.hour == 2

    def test_24_hour_clock(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=UTC).astimezone()
        parsed = providers.parse_retry_at("limit resets at 15:45", now=now)
        assert (parsed.hour, parsed.minute) == (15, 45)

    def test_relative_duration(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=UTC).astimezone()
        parsed = providers.parse_retry_at("resets in 42 minutes", now=now)
        assert 41 <= (parsed - now).total_seconds() / 60 <= 43

    def test_explicit_iso_timestamp(self):
        parsed = providers.parse_retry_at("available again at 2026-01-05T15:00:00Z")
        assert parsed is not None and parsed.hour == 15

    def test_unparseable_returns_none(self):
        assert providers.parse_retry_at("limit reached, try later") is None
        assert providers.parse_retry_at("") is None

    def test_unknown_reset_uses_bounded_backoff(self, conn):
        """No reset time must mean patient backoff, never a busy poll."""
        first = providers.begin_cooldown(conn, "claude-code", reason="limit")
        assert first.seconds_remaining() >= 60

        providers.begin_cooldown(conn, "claude-code", reason="limit")
        third = providers.begin_cooldown(conn, "claude-code", reason="limit")
        assert third.consecutive == 3
        assert third.seconds_remaining() <= providers.MAX_BACKOFF_SECONDS


class TestProviderCooldown:
    def test_cooldown_is_recorded_with_full_provenance(self, conn):
        state = providers.begin_cooldown(
            conn, "claude-code", reason="subscription allowance exhausted",
            raw_message=CLAUDE_USAGE_LIMIT,
            retry_at=datetime.now(UTC) + timedelta(hours=2),
        )
        assert state.status == providers.COOLDOWN
        assert state.raw_message == CLAUDE_USAGE_LIMIT
        assert state.detected_at and state.retry_at

    def test_cooldown_survives_a_reconnect(self, project_root):
        conn = db.connect(project_root)
        try:
            providers.begin_cooldown(
                conn, "claude-code", reason="limit",
                retry_at=datetime.now(UTC) + timedelta(hours=3),
            )
        finally:
            conn.close()

        conn = db.connect(project_root)
        try:
            assert not providers.is_available(conn, "claude-code")
        finally:
            conn.close()

    def test_cooldown_lifts_when_retry_at_passes(self, conn):
        providers.begin_cooldown(
            conn, "claude-code", reason="limit",
            retry_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert not providers.is_available(conn, "claude-code")

        later = datetime.now(UTC) + timedelta(seconds=5)
        assert not providers.is_available(conn, "claude-code", now=later)
        assert providers.refresh(conn, later, checker=lambda _: {"available": True})
        assert providers.is_available(conn, "claude-code")

    def test_one_account_cooldown_does_not_touch_another_provider(self, conn):
        providers.begin_cooldown(conn, "claude-code", reason="limit")
        assert not providers.is_available(conn, "claude-code")
        assert providers.is_available(conn, "codex")

    def test_shared_account_shares_the_cooldown(self, conn):
        """Two Claude workers on one login share one allowance."""
        providers.begin_cooldown(conn, "claude-code", reason="limit", account="personal")
        assert not providers.is_available(conn, "claude-code", "personal")
        assert providers.is_available(conn, "claude-code", "work")


class TestQuotaPause:
    def test_worker_failure_cools_provider_and_pauses_task(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        kind = quota.handle_worker_failure(
            conn, task_id, "claude-code", CLAUDE_USAGE_LIMIT, 1
        )
        assert kind == errors.USAGE_LIMIT
        assert not providers.is_available(conn, "claude-code")

        task = db.get_task(conn, task_id)
        assert task["status"] == sm.BLOCKED
        assert quota.is_quota_paused(task)

    def test_pause_does_not_touch_the_attempt_counter(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        before = int(db.get_task(conn, task_id)["attempts"])
        quota.handle_worker_failure(conn, task_id, "claude-code", CLAUDE_USAGE_LIMIT, 1)
        assert int(db.get_task(conn, task_id)["attempts"]) == before

    def test_metadata_records_why_and_until_when(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        quota.handle_worker_failure(conn, task_id, "claude-code", CLAUDE_USAGE_LIMIT, 1)

        meta = quota.blocked_meta(db.get_task(conn, task_id))
        assert meta["reason"] == quota.REASON
        assert meta["provider"] == "claude-code"
        assert meta["retry_at"]
        assert "usage limit" in meta["raw_message"].lower()

    def test_worktree_and_checkpoint_are_preserved(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        db.update_task(conn, task_id, worktree="/tmp/wt-retry", branch="agent/retry",
                       base_sha="abc1234")
        db.write_checkpoint(conn, task_id, {"completed": ["half the work"]},
                            kind="mechanical")

        quota.handle_worker_failure(conn, task_id, "claude-code", CLAUDE_USAGE_LIMIT, 1)

        task = db.get_task(conn, task_id)
        assert task["worktree"] == "/tmp/wt-retry"
        assert task["branch"] == "agent/retry"
        assert task["base_sha"] == "abc1234"
        assert db.latest_checkpoint(conn, task_id) is not None

    def test_task_error_is_not_treated_as_a_provider_problem(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        assert quota.handle_worker_failure(
            conn, task_id, "claude-code", "AssertionError: expected 3", 1
        ) is None
        assert providers.is_available(conn, "claude-code")


class TestLeaseHandlingWhilePaused:
    def test_contested_lease_is_reserved(self, conn, make_task):
        """Releasing would let another task edit half-finished work."""
        paused = make_task("split media", ["services/media.py"])
        db.acquire_leases(conn, paused, ["services/media.py"])
        db.create_task(conn, spec_id="rival", title="also wants media",
                       expected_write=["services/media.py"], status=sm.PLANNED)

        result = quota.pause(conn, paused, provider="claude-code", retry_at=None)
        assert result.lease_kept
        assert any(int(x["task_id"]) == paused for x in db.active_leases(conn))

    def test_uncontested_lease_is_released(self, conn, make_task):
        paused = make_task("split media", ["services/media.py"])
        db.acquire_leases(conn, paused, ["services/media.py"])

        result = quota.pause(conn, paused, provider="claude-code", retry_at=None)
        assert not result.lease_kept

    def test_reserved_lease_still_blocks_another_task(self, conn, project, make_task):
        from agentkit.leases import decide

        paused = make_task("split media", ["services/media.py"])
        db.acquire_leases(conn, paused, ["services/media.py"])
        other = db.create_task(conn, spec_id="rival", title="wants media",
                               expected_write=["services/media.py"], status=sm.RUNNING)
        quota.pause(conn, paused, provider="claude-code", retry_at=None)

        verdict = decide(conn, project, "services/media.py", other)
        assert not verdict.allowed, "a paused task's hotspot must stay reserved"

    def test_reserved_lease_does_not_expire_during_a_long_cooldown(self, conn, make_task):
        paused = make_task("split media", ["services/media.py"])
        db.acquire_leases(conn, paused, ["services/media.py"], ttl_seconds=1)
        db.create_task(conn, spec_id="rival", title="wants media",
                       expected_write=["services/media.py"], status=sm.RUNNING)
        quota.pause(conn, paused, provider="claude-code", retry_at=None)

        assert db.expire_stale_leases(conn) == []
        assert any(int(x["task_id"]) == paused for x in db.active_leases(conn))


class TestAutomaticWake:
    def test_task_resumes_when_the_provider_recovers(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        providers.begin_cooldown(
            conn, "claude-code", reason="limit",
            retry_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        quota.pause(conn, task_id, provider="claude-code",
                    retry_at=providers.get_state(conn, "claude-code").retry_at)

        assert quota.wake_ready(conn) == []          # too early

        later = datetime.now(UTC) + timedelta(seconds=30)
        providers.refresh(conn, later, checker=lambda _: {"available": True})
        assert quota.wake_ready(conn, later) == [task_id]

        task = db.get_task(conn, task_id)
        assert task["status"] == sm.READY
        assert task["blocked_meta"] is None

    def test_wake_is_recorded(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        providers.begin_cooldown(conn, "claude-code", reason="limit",
                                 retry_at=datetime.now(UTC) - timedelta(seconds=1))
        quota.pause(conn, task_id, provider="claude-code", retry_at=None)
        providers.refresh(conn, datetime.now(UTC) + timedelta(hours=9), checker=lambda _: {"available": True})
        quota.wake_ready(conn, datetime.now(UTC) + timedelta(hours=9))

        kinds = [e["kind"] for e in db.recent_events(conn, task_id, limit=20)]
        assert "quota_paused" in kinds
        assert "quota_resumed" in kinds

    def test_other_providers_tasks_are_untouched(self, conn, make_task):
        claude_task = make_task("claude work", ["services/a.py"])
        codex_task = make_task("codex work", ["services/b.py"])
        quota.handle_worker_failure(
            conn, claude_task, "claude-code", CLAUDE_USAGE_LIMIT, 1
        )
        assert db.get_task(conn, codex_task)["status"] == sm.RUNNING


class TestProviderIsolationInScheduling:
    """One account cooling must not stop work that does not need it."""

    def test_cooling_provider_is_skipped_but_capable_one_is_used(self):
        capabilities = {"claude-code": _caps("claude-code"), "codex": _caps("codex")}
        cooling = {"claude-code": "in cooldown for ~180 min (usage limit)"}
        name, reason = scheduler.choose_adapter(
            {"kind": "SAFE_PARALLEL"}, capabilities, unavailable=cooling
        )
        assert name == "codex"

    def test_no_available_capable_provider_reports_the_cooldown(self):
        capabilities = {"claude-code": _caps("claude-code")}
        cooling = {"claude-code": "in cooldown for ~180 min (usage limit)"}
        name, reason = scheduler.choose_adapter(
            {"kind": "SAFE_PARALLEL"}, capabilities, unavailable=cooling
        )
        assert name is None
        assert reason.startswith("provider unavailable")

    def test_capability_rules_are_never_relaxed_to_dodge_a_cooldown(self):
        """A cooling strong provider must not fall through to a weak one."""
        capabilities = {
            "claude-code": _caps("claude-code"),
            "weak": _caps("weak", workspace_sandbox=False),
        }
        cooling = {"claude-code": "in cooldown"}
        name, reason = scheduler.choose_adapter(
            {"kind": "HOTSPOT"}, capabilities, unavailable=cooling
        )
        assert name is None
        assert "weak lacks" in reason or "provider unavailable" in reason

    def test_unavailable_adapters_reads_from_the_database(self, conn):
        providers.begin_cooldown(conn, "claude-code", reason="usage limit",
                                 retry_at=datetime.now(UTC) + timedelta(hours=2))
        cooling = scheduler.unavailable_adapters(conn)
        assert "claude-code" in cooling
        assert "codex" not in cooling


class TestWatchLoop:
    def test_watch_stops_when_the_queue_is_empty(self, project_root):
        state = watch.run(project_root, max_iterations=3, sleeper=lambda _s: None)
        assert state.stopped_reason
        assert state.iterations >= 1

    def test_watch_does_not_relaunch_during_a_cooldown(self, project_root, conn):
        """Restart-safety: a fresh loop reloads the wait instead of ignoring it."""
        task_id = db.create_task(
            conn, spec_id="a", title="claude work",
            expected_write=["services/retry.py"], status=sm.RUNNING,
        )
        providers.begin_cooldown(
            conn, "claude-code", reason="usage limit",
            retry_at=datetime.now(UTC) + timedelta(hours=4),
        )
        quota.pause(conn, task_id, provider="claude-code",
                    retry_at=providers.get_state(conn, "claude-code").retry_at)
        conn.close()

        state = watch.run(project_root, max_iterations=2, sleeper=lambda _s: None)

        check = db.connect(project_root)
        try:
            task = db.get_task(check, task_id)
            assert task["status"] == sm.BLOCKED, "must not relaunch into a live cooldown"
            assert quota.is_quota_paused(task)
            assert not providers.is_available(check, "claude-code")
        finally:
            check.close()
        assert state.launched == 0

    def test_sleep_interval_tracks_the_cooldown(self, conn):
        providers.begin_cooldown(conn, "claude-code", reason="limit",
                                 retry_at=datetime.now(UTC) + timedelta(seconds=45))
        assert watch.next_sleep(conn, 300, launched=False) <= 45

    def test_sleep_is_short_while_work_is_moving(self, conn):
        assert watch.next_sleep(conn, 300, launched=True) == watch.MIN_SLEEP_SECONDS

    def test_idle_reason_ignores_quota_paused_work(self, conn, make_task):
        """A quota wait is not idleness — the loop must keep waiting."""
        task_id = make_task("add retry", ["services/retry.py"])
        providers.begin_cooldown(conn, "claude-code", reason="limit",
                                 retry_at=datetime.now(UTC) + timedelta(hours=2))
        quota.pause(conn, task_id, provider="claude-code", retry_at=None)
        assert watch.idle_reason(conn) is None


class TestStatusOutput:
    def test_providers_section_shows_the_reset_time(self, conn):
        from agentkit import observability

        providers.begin_cooldown(
            conn, "claude-code", reason="usage limit",
            retry_at=datetime.now(UTC) + timedelta(hours=2),
        )
        rendered = observability.render_providers(conn)
        assert "claude-code" in rendered
        assert "COOLDOWN" in rendered
        assert "resets" in rendered

    def test_status_explains_why_a_task_waits(self, conn, make_task):
        from agentkit import observability

        task_id = make_task("add retry", ["services/retry.py"])
        quota.handle_worker_failure(conn, task_id, "claude-code", CLAUDE_USAGE_LIMIT, 1)

        rendered = observability.render_status(conn)
        assert "PROVIDERS" in rendered
        assert "TASKS" in rendered
        assert "usage limit" in rendered
        assert "waiting on quota" in rendered
