"""Commit messages name the user as the only author.

Claude Code adds a `Co-Authored-By: Claude ...` trailer to the commits it writes,
so every worker commit in a pilot project carried one. The user is the sole author of
their projects; the host commit path removes attribution trailers and the
"Generated with" footer from every worker message before Git sees it.
"""
from __future__ import annotations

import re

_ATTRIBUTION = re.compile(r"^\s*(co-authored-by:.*|\W*generated with \[?claude code\]?.*)$", re.IGNORECASE)


def without_attribution(message: str) -> str:
    """The message with co-author trailers and tool footers removed; nothing else changes."""
    if not isinstance(message, str):
        return message
    kept = [line for line in message.splitlines() if not _ATTRIBUTION.match(line)]
    return "\n".join(kept).rstrip() + ("\n" if message.endswith("\n") and kept else "")
