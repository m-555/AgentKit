"""Real quota wording and manual reset evidence; no provider inference."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentkit import adapters, errors, providers, supervisor


@pytest.mark.parametrize("text", ["You\u2019ve hit your usage limit. Upgrade to Pro", json.dumps({"type": "error", "message": "You\u2019ve hit your usage limit. Upgrade to Pro"})])
def test_actual_codex_unicode_limit_is_not_a_task_crash(text):
    result = adapters.get("codex").classify_error(text, 1)
    assert result.kind == errors.USAGE_LIMIT
    assert not result.consumes_attempt
    assert result.raw == text


def test_manual_reset_checks_metadata_before_old_weekly_deadline(conn, project, monkeypatch):
    now = datetime.now(UTC)
    providers.begin_cooldown(conn, "codex", reason="weekly limit", retry_at=now + timedelta(days=5))
    conn.execute("UPDATE provider_state SET detected_at=? WHERE account_key=?", ((now-timedelta(seconds=301)).isoformat(), providers.account_key("codex")))
    calls = []
    def check():
        calls.append(1)
        return {"available": True, "complete": True, "windows": [{"window": "10080", "used_percent": 0}]}
    monkeypatch.setattr(adapters, "get", lambda *a: SimpleNamespace(account_id=lambda: "default", availability_requires_inference=False, check_availability=check))
    supervisor.refresh_accounts(conn, project, ["codex", "codex"])
    assert providers.is_available(conn, "codex") and len(calls) == 1
    supervisor.refresh_accounts(conn, project, ["codex"])
    assert len(calls) == 1


def test_claude_cooldown_does_not_spend_inference_before_reset(conn, project, monkeypatch):
    providers.begin_cooldown(conn, "claude-code", reason="limit", retry_at=datetime.now(UTC)+timedelta(hours=2))
    monkeypatch.setattr(adapters, "get", lambda *a: SimpleNamespace(account_id=lambda: "default", availability_requires_inference=True, check_availability=lambda: pytest.fail("paid early check")))
    supervisor.refresh_accounts(conn, project, ["claude-code"])
    assert not providers.is_available(conn, "claude-code")
