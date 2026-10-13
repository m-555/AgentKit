"""F32: a local builder writes in parts, so an output-capped response cannot lose the whole file.

Qwen's first standard backend task (a 260-line router) read its five inputs in
two steps, then spent nine minutes in one response drafting the whole file. The
response stopped at the 24,576-token output cap before its write call, so the
worktree stayed at its base and the task failed with nothing to preserve. The
builder's standing rules, which OpenCode re-sends on every request, now require
a short skeleton first and one function, endpoint or test per edit.
"""
from __future__ import annotations

from agentkit.adapters import local_opencode


def test_builders_are_told_to_write_in_parts():
    task = {"id": 150, "kind": "DEPENDENT", "title": "Finishing routes",
            "expected_write": ["routes/creative_workflow_finishing.py"]}
    rules = local_opencode.standing_rules(task, None)
    assert local_opencode.WRITE_IN_PARTS in rules
    assert "never a whole file in one tool call" in rules and "output limit" in rules


def test_research_workers_get_no_writing_rule():
    rules = local_opencode.standing_rules({"id": 1, "kind": "RESEARCH", "title": "Read"}, None)
    assert local_opencode.WRITE_IN_PARTS not in rules
