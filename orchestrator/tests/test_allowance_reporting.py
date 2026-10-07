"""Account allowance and session counters remain distinct, accurate and passive."""

import json
from datetime import UTC, datetime

import pytest

from agentkit import dashboard, session_usage
from agentkit.quota_report import reports

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)


def view(used=26, observed="2026-10-02T19:59:00+00:00", reset="2026-10-02T21:00:00+00:00"):
    return {
        "providers": [
            {"account_key": "codex:default", "provider": "codex", "status": "AVAILABLE"}
        ],
        "quota_windows": [
            {
                "account_key": "codex:default",
                "bucket": "codex",
                "window": "300",
                "used_percent": used,
                "observed_at": observed,
                "resets_at": reset,
            }
        ],
    }


def test_account_percentage_is_reported_and_not_derived_from_session_tokens():
    data = view()
    data["processes"] = [{"usage": {"output_tokens": 10000000}}]
    row = reports(data, NOW)[0]
    assert row["remaining_percent"] == 74 and not row["stale"]
    assert row["source"] == "reported_account_window"


@pytest.mark.parametrize("used", [None, True, -1, 101, float("nan"), "26"])
def test_unknown_or_invalid_percentage_is_not_zero_or_unlimited(used):
    row = reports(view(used), NOW)[0]
    assert row["used_percent"] is None and row["remaining_percent"] is None


def test_reset_elapsed_does_not_claim_recovery():
    row = reports(view(100, reset="2026-10-02T19:58:00+00:00"), NOW)[0]
    assert row["used_percent"] == 100 and row["reset_due"]
    assert reports(view(observed="2026-10-02T18:00:00+00:00"), NOW)[0]["stale"]


def test_allowance_buckets_and_windows_are_never_collapsed():
    data = view()
    data["quota_windows"].append(
        {
            **data["quota_windows"][0],
            "bucket": "codex_other",
            "window": "10080",
            "used_percent": 90,
        }
    )
    assert [row["remaining_percent"] for row in reports(data, NOW)] == [74, 10]


def test_auth_availability_without_usage_is_not_an_allowance_percentage():
    data = view()
    data["quota_windows"] = []
    row = reports(data, NOW)[0]
    assert row["source"] == "availability_only" and row["remaining_percent"] is None


def test_claude_session_total_and_turn_count_survive_partial_log_tail(tmp_path, monkeypatch):
    folder = tmp_path / ".ai/runtime/process-1"
    folder.mkdir(parents=True)
    event = {
        "type": "result",
        "num_turns": 75,
        "usage": {
            "input_tokens": 108,
            "output_tokens": 323493,
            "cache_read_input_tokens": 12048377,
            "cache_creation_input_tokens": 391759,
            "output_tokens_details": {"thinking_tokens": 272872},
        },
    }
    path = folder / "events.jsonl"
    path.write_text(" " * 3000 + "\n" + json.dumps(event) + "\n")
    monkeypatch.setattr(session_usage, "MAX_READ_BYTES", 2000)
    result = session_usage.read_details(tmp_path, 1)
    assert result["agent_turns"] == 75 and result["counter_scope"] == "session_total"
    assert result["output_tokens"] == 323493 and result["thinking_tokens"] == 272872
    assert not result["stream_complete"]
    shown = dashboard.read_usage(tmp_path, 1)
    assert shown["agent_turns"] == 75 and shown["counter_scope"] == "session_total"
