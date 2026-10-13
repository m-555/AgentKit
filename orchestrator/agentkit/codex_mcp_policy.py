"""Approve bounded AgentKit lifecycle calls without enabling approval prompts.

The MCP server enforces task identity, generation, leases and exact ownership.
Filesystem sandboxing and hook enforcement remain independent of this policy.
"""
from __future__ import annotations

# graph_amend records a proposal only; it cannot widen a lease or edit the graph.
# Kept equal to mcp_worker.WORKER_TOOLS, the tools a worker's MCP server exposes.
WORKER_TOOLS = ("brief", "checkpoint", "task_status", "task_commit", "commit_and_gate", "gate_run",
                "lease_check", "audit_diff", "gate_list", "lease_request", "graph_amend", "job_attachment")
READ_TOOLS = ("brief", "job_brief", "task_list", "task_diff", "source_read",
              "source_list", "source_search", "source_diff", "gate_list", "job_attachment")

# These endpoints authenticate the live coordinator and current job server-side.
# No whole-server approval, global job creation/start, worker writes or commits.
COORDINATOR_TOOLS = ("job_decision", "task_define", "job_plan_ready", "job_accept",
                     "job_block", "task_requeue", "integration_retry", "amendment_resolve",
                     "project_configure", "manager_audit", "manager_ack", "manager_messages")


def approval_flags(role: str, *, readonly: bool) -> list[str]:
    """Only known role-specific host calls bypass interactive MCP approval."""
    tools: tuple[str, ...] = READ_TOOLS if readonly else WORKER_TOOLS
    if role == "reviewer":
        tools += ("audit_diff", "gate_run", "review_submit", "job_review_submit")
    elif role == "coordinator" and readonly:
        tools += COORDINATOR_TOOLS
    # Unknown/control mutation calls still require operator approval. Do not
    # approve an entire server or relax the worker's approval_policy=never.
    flags = ["-c", 'mcp_servers.agentkit.default_tools_approval_mode="prompt"']
    for tool in tools:
        flags += ["-c", f'mcp_servers.agentkit.tools.{tool}.approval_mode="approve"']
    return flags
