"""Worker commits carry no co-author trailer: the user is the only author.

Claude Code workers ended every pilot-project commit with "Co-Authored-By: Claude Opus
5.5"; the user asked that their commits name only them. Both host commit paths
remove attribution before Git sees the message.
"""
from __future__ import annotations

import inspect

import pytest

from agentkit import host_completion, mcp_workspaces
from agentkit.commit_message import without_attribution

TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


@pytest.mark.parametrize("message, expected", [
    (f"Exclude audio rows\n\nScene Studio is unchanged.\n\n{TRAILER}", "Exclude audio rows\n\nScene Studio is unchanged."),
    (f"Fix it\n\n{TRAILER}\nco-authored-by: Someone <a@b.c>\n", "Fix it\n"),
    ("Add view\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)", "Add view"),
    ("Plain message", "Plain message"),
    ("Mention the Co-Authored-By rule in prose", "Mention the Co-Authored-By rule in prose"),
])
def test_attribution_is_removed_and_nothing_else(message, expected):
    assert without_attribution(message) == expected


def test_a_message_that_was_only_a_trailer_is_refused_as_empty():
    assert without_attribution(TRAILER).strip() == ""


@pytest.mark.parametrize("path", [mcp_workspaces.task_commit, host_completion.complete])
def test_both_host_commit_paths_strip_attribution_first(path):
    source = inspect.getsource(path)
    assert source.index("without_attribution(message)") < source.index("len(message) > 2000")
