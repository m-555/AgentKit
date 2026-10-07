"""Approve bounded AgentKit lifecycle calls without enabling approval prompts.

The MCP server enforces task identity, generation, leases and exact ownership.
Filesystem sandboxing and hook enforcement remain independent of this policy.
"""
from __future__ import annotations

# graph_amend records a proposal only; it cannot widen a lease or edit the graph.
WORKER_TOOLS = ("brief", "checkpoint", "task_status", "task_commit", "gate_run",
                "lease_check", "audit_diff", "gate_list", "lease_request", "graph_amend")
READ_TOOLS = ("brief", "job_brief", "task_list", "task_diff", "source_read",
              "source_list", "source_search", "source_diff", "gate_list")


def approval_flags(role: str, *, readonly: bool) -> list[str]:
    """Only known role-specific host calls bypass interactive MCP approval."""
    tools: tuple[str, ...] = READ_TOOLS if readonly else WORKER_TOOLS
    if role == "reviewer":
        tools += ("review_submit",)
    # Unknown/control mutation calls still require operator approval. Do not
    # approve an entire server or relax the worker's approval_policy=never.
    flags = ["-c", 'mcp_servers.agentkit.default_tools_approval_mode="prompt"']
    for tool in tools:
        flags += ["-c", f'mcp_servers.agentkit.tools.{tool}.approval_mode="approve"']
    return flags
