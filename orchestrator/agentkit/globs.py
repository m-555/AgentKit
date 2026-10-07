"""Glob matching for ownership rules.

Ownership patterns are written by humans in `tasks.yaml` and `project.yaml`,
so they need to behave the way people expect rather than the way `fnmatch` does:

    routes/video.py      matches exactly that file
    routes/video/        matches everything under that directory
    routes/**            matches everything under routes/
    services/**/*.py     matches .py files at any depth under services/
    *.py                 matches .py files at the top level only

The important departure from `fnmatch` is that `*` does not cross a directory
separator, while `**` does. `fnmatch` treats `*` as matching everything, which
would make `routes/*` silently own the whole subtree.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .paths import normalize


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    pat = normalize(pattern)
    if pat.endswith("/"):
        pat = pat + "**"
    out: list[str] = []
    i = 0
    while i < len(pat):
        char = pat[i]
        if char == "*":
            if pat.startswith("**/", i):
                # `**/` may match zero directories, so `a/**/b` matches `a/b`.
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def matches(pattern: str, path: str) -> bool:
    """True when `path` (relative, forward slashes) falls under `pattern`."""
    target = normalize(path)
    if not target or not pattern:
        return False
    pat = normalize(pattern)
    if _compiled(pat).match(target):
        return True
    # A bare directory name owns its contents: `routes` covers `routes/video.py`.
    return "*" not in pat and "?" not in pat and target.startswith(pat + "/")


def matches_any(patterns: list[str] | tuple[str, ...], path: str) -> str | None:
    """Return the first pattern that matches, or None."""
    for pattern in patterns or ():
        if matches(pattern, path):
            return pattern
    return None


def overlaps(a: str, b: str) -> bool:
    """Conservative test for whether two ownership patterns can collide.

    Used before handing two tasks to two agents. It errs towards reporting an
    overlap: a false positive costs a little concurrency, a false negative costs
    a corrupted merge.
    """
    pa, pb = normalize(a), normalize(b)
    if pa == pb:
        return True
    if matches(pa, pb) or matches(pb, pa):
        return True
    # Compare the literal prefixes that precede any wildcard.
    head_a = pa.split("*", 1)[0].rstrip("/")
    head_b = pb.split("*", 1)[0].rstrip("/")
    if not head_a or not head_b:
        return True
    if head_a == head_b:
        return True
    return head_a.startswith(head_b + "/") or head_b.startswith(head_a + "/")
