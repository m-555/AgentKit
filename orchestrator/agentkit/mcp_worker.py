"""Worker-facing MCP surface: fewer tools and fewer turns (token efficiency).

Every model turn resends every tool schema and the whole context. The full
server exposes 41 tools (about 7k tokens); a builder or tester uses about ten,
and Codex loads every MCP schema into each turn. A worker's server therefore
keeps only its own tools, and commit_and_gate does the host commit and the
declared gate in one call instead of two turns. Coordinators, reviewers and
the external manager keep the full set.
"""
from __future__ import annotations

import os

WORKER_TOOLS = frozenset({
    "brief", "checkpoint", "task_status", "task_commit", "commit_and_gate", "gate_run", "gate_list",
    "audit_diff", "lease_check", "lease_request", "graph_amend", "job_attachment",
})


def commit_and_gate(message: str) -> str:
    """Commit your owned files through the host, then run your declared gate: one call, not two.

    A refused commit stops here with its reason; nothing is checked. Report the
    gate output as REVIEW evidence, or fix your own files and call this again.
    """
    from . import mcp_commit_errors, mcp_server
    committed = mcp_commit_errors.task_commit(message)
    return f"{committed}\n\n{mcp_server.gate_run()}"


def register(server) -> None:
    """Register last: a worker session then keeps only the tools it uses."""
    server.add_tool(commit_and_gate)
    if os.environ.get("AGENTKIT_ROLE") != "worker":
        return
    for name in list(server._tool_manager._tools):
        if name not in WORKER_TOOLS:
            server.remove_tool(name)
