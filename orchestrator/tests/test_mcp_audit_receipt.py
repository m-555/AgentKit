"""Recovery receipts stay small without weakening audit evidence or findings."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agentkit import mcp_manager


@pytest.fixture
def audited(monkeypatch, tmp_path):
    closed = []
    conn = SimpleNamespace(close=lambda: closed.append(True))
    report = {
        "job": {"revision": 4, "requests": ["large historical context" * 20000]},
        "tasks": [{"id": value} for value in range(100)],
        "integration": {"head": "abc123"},
        "runtime": {"status": "ACTIVE"},
    }
    receipt = {"job": "demo", "epoch": 7, "digest": "exact-digest",
               "passed": False, "findings": ["task 8: owner still alive"], "report": report}
    monkeypatch.setattr(mcp_manager, "_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_manager.db, "connect", lambda root: conn)
    monkeypatch.setattr(mcp_manager, "credential", lambda root, job: "credential")
    monkeypatch.delenv("AGENTKIT_PROCESS", raising=False)

    def audit(connection, root, job, *, token):
        assert connection is conn and root == tmp_path and job == "demo"
        assert token == "credential"
        return copy.deepcopy(receipt)

    monkeypatch.setattr(mcp_manager.manager_audit, "run", audit)
    return receipt, closed


def test_default_receipt_omits_history_but_keeps_exact_failure_and_ack_fields(audited):
    receipt, closed = audited
    text = mcp_manager.manager_audit_run("demo")
    result = json.loads(text)
    for key in ("job", "epoch", "digest", "passed", "findings"):
        assert result[key] == receipt[key]
    assert "report" not in result and "historical context" not in text
    assert len(text) < 700
    assert result["summary"] == {
        "integration_head": "abc123", "task_count": 100,
        "job_revision": 4, "runtime_status": "ACTIVE",
    }
    assert result["evidence_storage"] == "manager_state.audit_report"
    assert len(receipt["report"]["tasks"]) == 100
    assert closed == [True]


def test_full_report_requires_an_explicit_request(audited):
    receipt, closed = audited
    assert json.loads(mcp_manager.manager_audit_run("demo", include_report=True)) == receipt
    assert closed == [True]


def test_failed_audit_still_closes_connection(audited, monkeypatch):
    _, closed = audited

    def fail(*args, **kwargs):
        raise PermissionError("wrong manager")

    monkeypatch.setattr(mcp_manager.manager_audit, "run", fail)
    with pytest.raises(PermissionError, match="wrong manager"):
        mcp_manager.manager_audit_run("demo")
    assert closed == [True]
