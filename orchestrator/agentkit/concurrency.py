"""How many workers run at once: decided by the task graph, optionally capped.

AgentKit does not impose a total agent count. `max_workers: 0` (the default)
launches every eligible independent task. What actually limits concurrency is
safety, not a number:

* exclusive path leases and predicted write-set overlap serialise shared files;
* dependencies wait for integrated (DONE) prerequisites, and frozen contracts
  must exist before their consumers start;
* one task has at most one live session, claimed before a process starts;
* inference resources such as the single local GPU slot are reserved per pass;
* every change still needs an independent reviewer before integration.

A positive value is an optional cap, for example to spread one account's quota
over a longer period. Parallel workers on one account consume that account's
allowance faster; concurrency never creates quota.
"""
from __future__ import annotations

import argparse
from typing import Any

UNLIMITED = 0


def validate(value: Any, where: str = "max_workers") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a whole number: 0 for unlimited or a positive cap")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a whole number: 0 for unlimited or a positive cap") from None
    if number != value and not isinstance(value, str):
        raise ValueError(f"{where} must be a whole number: 0 for unlimited or a positive cap")
    if number < 0:
        raise ValueError(f"{where} cannot be negative; use 0 for unlimited or a positive cap")
    return number


def argument(value: str) -> int:
    """argparse `type=` for --max-workers."""
    try:
        return validate(value, "--max-workers")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def configured(project) -> int:
    raw = getattr(project, "raw", {}) or {}
    nested = raw.get("concurrency") or {}
    value = raw.get("max_workers", nested.get("max_workers", UNLIMITED) if isinstance(nested, dict) else UNLIMITED)
    return validate(value, "max_workers in .ai/project.yaml")


def resolve(project, requested: int | None) -> int | None:
    """The effective cap, or None for unlimited. An explicit request wins over config."""
    value = configured(project) if requested is None else validate(requested)
    return None if value == UNLIMITED else value


def describe(limit: int | None) -> str:
    return "unlimited (bounded by leases, dependencies and resources)" if limit is None else f"at most {limit}"
