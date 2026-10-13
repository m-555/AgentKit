"""Independent reviewers need scoped audit/check access and post-check identity fences."""
import pytest

from agentkit import db, gates, mcp_server
from agentkit.codex_mcp_policy import approval_flags


def test_review_approvals_include_audit_and_gates_without_worker_mutations():
    flags = approval_flags("reviewer", readonly=True)
    settings = dict(item.split("=", 1) for item in flags[1::2])
    for tool in ("audit_diff", "gate_run", "review_submit"):
        assert settings[f"mcp_servers.agentkit.tools.{tool}.approval_mode"] == '"approve"'
    for tool in ("task_commit", "task_status", "lease_request", "task_define"):
        assert f"mcp_servers.agentkit.tools.{tool}.approval_mode" not in settings


def review_task(conn, project_root, monkeypatch):
    identifier = db.create_task(conn, title="Review subject", status="REVIEW", generation=1,
                                role="backend-builder", gate_level="fast", worktree=str(project_root))
    for key, value in {"AGENTKIT_ROOT": str(project_root), "AGENTKIT_TASK": str(identifier),
                       "AGENTKIT_GENERATION": "1", "AGENTKIT_ROLE": "review"}.items():
        monkeypatch.setenv(key, value)
    def forbidden_worker(*args, **kwargs):
        pytest.fail("review must use control authority, not terminated builder authority")
    monkeypatch.setattr(mcp_server.worker, "require_current", forbidden_worker)
    monkeypatch.setattr(mcp_server.repo, "head_commit", lambda work: "exact-head")
    monkeypatch.setattr(mcp_server.repo, "is_clean", lambda work: True)
    return identifier


def test_review_gate_revalidates_control_before_and_after_check(conn, project_root, monkeypatch):
    identifier = review_task(conn, project_root, monkeypatch)
    checks = []
    def authority(connection, **kwargs):
        checks.append(kwargs)
    monkeypatch.setattr(mcp_server.processes, "require_control", authority)
    monkeypatch.setattr(mcp_server.gates, "run_gate", lambda *a, **k: gates.GateResult("fast", True))
    assert "passed" in mcp_server.gate_run()
    assert checks == [{"purposes": ("review",), "task_id": identifier}] * 2
    assert conn.execute("SELECT passed FROM gate_results WHERE task_id=?", (identifier,)).fetchone()[0] == 1


def test_review_owner_lost_during_check_cannot_record_receipt(conn, project_root, monkeypatch):
    identifier = review_task(conn, project_root, monkeypatch)
    checks = []
    def authority(connection, **kwargs):
        checks.append(kwargs)
        if len(checks) == 2:
            raise PermissionError("review session retired during check")
    monkeypatch.setattr(mcp_server.processes, "require_control", authority)
    monkeypatch.setattr(mcp_server.gates, "run_gate", lambda *a, **k: gates.GateResult("fast", True))
    with pytest.raises(PermissionError, match="retired"):
        mcp_server.gate_run()
    assert conn.execute("SELECT count(*) FROM gate_results WHERE task_id=?", (identifier,)).fetchone()[0] == 0


def test_unauthorized_review_cannot_execute_gate(conn, project_root, monkeypatch):
    review_task(conn, project_root, monkeypatch)
    def denied(*a, **k):
        raise PermissionError("reviewer belongs to another task")
    def forbidden(*a, **k):
        pytest.fail("unauthorized check must not execute")
    monkeypatch.setattr(mcp_server.processes, "require_control", denied)
    monkeypatch.setattr(mcp_server.gates, "run_gate", forbidden)
    with pytest.raises(PermissionError, match="another task"):
        mcp_server.gate_run()
