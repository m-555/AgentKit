"""Regression for noninteractive workers blocked before host commit."""
from agentkit.codex_mcp_policy import approval_flags


def policy(role, readonly):
    flags = approval_flags(role, readonly=readonly)
    return dict(item.split("=", 1) for item in flags[1::2])


def test_worker_approves_host_completion_but_not_global_mutators():
    settings = policy("frontend-builder", False)
    for tool in ("task_commit", "gate_run", "checkpoint", "task_status", "graph_amend"):
        assert settings[f"mcp_servers.agentkit.tools.{tool}.approval_mode"] == '"approve"'
    assert settings["mcp_servers.agentkit.default_tools_approval_mode"] == '"prompt"'
    for tool in ("job_start", "task_create", "task_define", "task_requeue", "amendment_resolve", "project_configure", "review_submit"):
        assert f"mcp_servers.agentkit.tools.{tool}.approval_mode" not in settings
    assert not any("sandbox" in key or key == "approval_policy" for key in settings)


def test_reviewer_cannot_approve_worker_edits_or_commits():
    settings = policy("reviewer", True)
    assert settings["mcp_servers.agentkit.tools.review_submit.approval_mode"] == '"approve"'
    for tool in ("task_commit", "task_status", "lease_request"):
        assert f"mcp_servers.agentkit.tools.{tool}.approval_mode" not in settings


def test_readonly_research_has_no_mutating_approval():
    settings = policy("researcher", True)
    assert settings["mcp_servers.agentkit.tools.source_read.approval_mode"] == '"approve"'
    assert "mcp_servers.agentkit.tools.review_submit.approval_mode" not in settings
