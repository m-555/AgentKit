"""Recorded tokens per process and per job, from host-written usage receipts.

Input is counted as the provider re-sent it: Codex input already includes its cached
part, Claude and local OpenCode report the cache separately, so it is added back.
Unknown counters stay unknown; nothing here estimates.
"""
from __future__ import annotations

from pathlib import Path

from . import usage_receipts
from .session_usage import read_usage


def counts(usage: dict | None, provider: str) -> tuple[int | None, int | None]:
    """(input including cache, output including thinking) for one session's counters."""
    if not usage:
        return None, None

    def value(key):
        number = usage.get(key)
        return number if type(number) is int and number >= 0 else None
    sent, output = value("input_tokens"), value("output_tokens")
    if sent is not None and provider in ("claude-code", "local-opencode"):
        sent += (value("cached_input_tokens") or 0) + (value("cache_write_input_tokens") or 0)
    return sent, output


def process(root, row) -> tuple[int | None, int | None]:
    """A process's counters: its receipt once stopped, its live log while running."""
    receipt = usage_receipts.read(Path(root), int(row["id"]))
    usage = receipt["usage"] if receipt else (
        read_usage(Path(root), int(row["id"])) if row["status"] in ("STARTING", "RUNNING") else None)
    return counts(usage, str(row["provider"]))


def job_total(conn, root, job_id: str) -> int | None:
    """Input plus output of every recorded session of the job; None when nothing is recorded."""
    total = None
    for row in conn.execute("SELECT id,provider,status FROM processes WHERE job_id=?", (job_id,)):
        sent, output = process(root, row)
        if sent is not None or output is not None:
            total = (total or 0) + (sent or 0) + (output or 0)
    return total
