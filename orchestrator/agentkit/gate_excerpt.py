"""The lines that say why a gate command failed, taken before its output is truncated (F15).

Gate output keeps only its tail. pytest prints FAILURES before warnings and the
short summary, Playwright prints the failing expectation before the web-server
log, so the tail alone often holds none of the cause. This picks assertion,
error and failure-header lines (with a little context) from the full output.
"""
from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MARKER = re.compile(
    r"^(E\s{2,}\S"                                   # pytest assertion detail
    r"|_{3,} \S.* _{3,}$"                            # pytest failing test header
    r"|\S+\.py:\d+: in \S"                           # pytest failing location
    r"|(FAILED|ERROR) \S"                            # pytest short summary
    r"|Traceback \(most recent call last\)"
    "|\\s*(FAIL|\u00d7|\u2718|\u2717)\\s"            # vitest / playwright: FAIL, multiplication, ballot X
    r"|\s*\d+\) \["                                  # playwright numbered failure
    r"|\s*(Error|AssertionError|TypeError|ReferenceError)\b.*"
    r"|\s*(Expected|Received|Locator|Call log)\b.*"
    r"|.*\berror TS\d+"                              # TypeScript
    r"|\S+Error: \S)")
_MAX_LINES = 60
_MAX_CHARS = 6000


def failure_excerpt(output: str) -> str:
    """Up to 60 lines (6000 characters) of failure detail, in order; empty when none is found."""
    lines = _ANSI.sub("", output).splitlines()
    picked: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if not _MARKER.match(line):
            continue
        # A pytest location line is followed by the source line it points at.
        window = lines[index:index + 2] if re.match(r"\S+\.py:\d+: in ", line) else [line]
        for item in window:
            text = item.rstrip()
            if text and text not in seen:
                seen.add(text)
                picked.append(text)
        if len(picked) >= _MAX_LINES:
            break
    excerpt = "\n".join(picked[:_MAX_LINES])
    return excerpt[:_MAX_CHARS]
