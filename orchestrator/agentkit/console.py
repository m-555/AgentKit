"""Making this tool's output survive a Windows console.

Every user-facing string here is written in ordinary prose, which means em dashes,
arrows and box-drawing characters. On Windows, Python picks the legacy ANSI code
page (cp1252) for stdout and stderr unless told otherwise, so those characters are
replaced with `?` or `�` before anyone sees them.

That is cosmetic in `agentkit doctor`. It is not cosmetic in a hook: `_block`
writes the refusal *to the model*, and a mangled explanation is a worse
explanation. So both streams are switched to UTF-8 at every entry point that
prints, with `errors="replace"` kept as a floor — a console that genuinely cannot
encode a glyph must still get the rest of the sentence rather than an exception
that takes the process down mid-write.

Deliberately not called from `mcp_server`: that process speaks JSON-RPC over
stdout and nothing should reach between it and its transport.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from typing import Any


def use_utf8() -> None:
    """Switch stdout and stderr to UTF-8. Safe to call more than once."""
    for stream_name in ("stdout", "stderr"):
        stream: Any = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # a plain file object, or already replaced
            continue
        # A stream that is closed, detached or not text-backed raises here; none of
        # those are worth failing a command over.
        with suppress(OSError, ValueError, AttributeError):
            reconfigure(encoding="utf-8", errors="replace")
