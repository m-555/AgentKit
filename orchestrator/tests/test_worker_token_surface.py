"""Token efficiency #1 and #3: a worker sees only its tools and needs fewer turns.

Every model turn resends every tool schema and the whole context. AgentKit's
server exposes 41 tools (about 7k tokens) and Codex loads every MCP schema into
each turn, while a builder or tester uses about ten. A worker's server keeps
only those; commit_and_gate replaces two turns (task_commit, then gate_run) with
one, and the worker prompt asks for all declared reads in a single turn.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agentkit import mcp_worker
from agentkit.mcp_errors import ToolError
from agentkit.scheduler import WORKER_PROMPT

LIST = ("import asyncio, json\nfrom agentkit import mcp_server\n"
        "tools = asyncio.run(mcp_server.mcp.list_tools())\n"
        "print(json.dumps({'names': sorted(t.name for t in tools), "
        "'chars': sum(len(json.dumps(t.model_dump(exclude_none=True))) for t in tools)}))")


def _tools(project_root, role):
    env = {**os.environ, "AGENTKIT_ROOT": str(project_root), "AGENTKIT_ROLE": role}
    out = subprocess.run([sys.executable, "-c", LIST], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_worker_server_exposes_only_worker_tools(project_root):
    listed = _tools(project_root, "worker")
    assert set(listed["names"]) == mcp_worker.WORKER_TOOLS
    assert listed["chars"] < 12000  # the full set is about 28000 characters


@pytest.mark.parametrize("role", ["coordinator", "review"])
def test_control_sessions_keep_the_full_set(project_root, role):
    names = set(_tools(project_root, role)["names"])
    assert {"manager_audit", "task_diff", "review_submit", "commit_and_gate"} <= names and len(names) > 30


def test_commit_and_gate_commits_then_runs_the_declared_gate(monkeypatch):
    calls = []
    monkeypatch.setattr("agentkit.mcp_commit_errors.task_commit", lambda message: calls.append(("commit", message)) or "Committed abc123")
    monkeypatch.setattr("agentkit.mcp_server.gate_run", lambda: calls.append(("gate",)) or "gate `unit` passed (1 command(s))")
    result = mcp_worker.commit_and_gate("Add the resolver")
    assert calls == [("commit", "Add the resolver"), ("gate",)]
    assert "Committed abc123" in result and "gate `unit` passed" in result


def test_a_refused_commit_never_runs_the_gate(monkeypatch):
    def refuse(message):
        raise ToolError("Host commit blocked: outside lease")
    monkeypatch.setattr("agentkit.mcp_commit_errors.task_commit", refuse)
    monkeypatch.setattr("agentkit.mcp_server.gate_run", lambda: pytest.fail("gate after a refused commit"))
    with pytest.raises(ToolError, match="outside lease"):
        mcp_worker.commit_and_gate("Add the resolver")


def test_the_worker_prompt_spends_fewer_turns():
    assert "commit_and_gate" in WORKER_PROMPT
    assert "in one turn" in WORKER_PROMPT
    assert "mcp__agentkit__commit_and_gate" in WORKER_PROMPT  # one ToolSearch loads every tool it needs


def test_codex_approves_exactly_the_worker_tools():
    from agentkit import codex_mcp_policy
    assert set(codex_mcp_policy.WORKER_TOOLS) == mcp_worker.WORKER_TOOLS
