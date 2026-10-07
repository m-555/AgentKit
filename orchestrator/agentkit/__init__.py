"""AgentKit orchestrator: task graph, ownership leases, checkpoints and gates.

The package is deliberately dependency-light. Everything an agent can reach goes
through :mod:`agentkit.mcp_server`; everything a hook can reach goes through
:mod:`agentkit.hooks_cli`. Only this package touches the SQLite database.
"""

__version__ = "0.2.0"
