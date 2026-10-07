"""Project-wide recorded usage, independent of dashboard pagination and task state."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import db, usage_receipts
from .session_usage import read_details

FIELDS = usage_receipts.FIELDS


def _empty() -> dict:
    return {"launches": 0, "reported_launches": 0, "complete_launches": 0,
            "partial_launches": 0, "missing_launches": 0,
            "recorded_tokens": None, "normalized_input_tokens": None,
            "counters": dict.fromkeys(FIELDS),
            "reported_counts": dict.fromkeys(FIELDS, 0)}


def _add(total: dict, usage: dict, provider: str) -> None:
    total["launches"] += 1
    values = {key: usage.get(key) if type(usage.get(key)) is int and usage[key] >= 0 else None
              for key in FIELDS}
    reported = any(value is not None for value in values.values())
    total["reported_launches"] += int(reported)
    total["missing_launches"] += int(not reported)
    # Claude input excludes cache reads/writes. Codex already includes its cache.
    keys = ["input_tokens"]
    if provider == "claude-code":
        keys.extend(("cached_input_tokens", "cache_write_input_tokens"))
    inputs = [values[key] for key in keys]
    supported = provider in ("claude-code", "codex")
    complete = (usage.get("complete") is True and supported and
                all(value is not None for value in [*inputs, values["output_tokens"]]))
    total["complete_launches"] += int(complete)
    total["partial_launches"] += int(reported and not complete)
    for key, value in values.items():
        if value is not None:
            total["counters"][key] = (total["counters"][key] or 0) + value
            total["reported_counts"][key] += 1
    if supported:
        known_inputs = [value for value in inputs if value is not None]
        if known_inputs:
            total["normalized_input_tokens"] = (total["normalized_input_tokens"] or 0) + sum(known_inputs)
        known = known_inputs + ([values["output_tokens"]] if values["output_tokens"] is not None else [])
        if known:
            total["recorded_tokens"] = (total["recorded_tokens"] or 0) + sum(known)


def snapshot(root: Path, live_usages: dict[int, dict] | None = None) -> dict:
    result = {**_empty(), "status": "ok", "providers": [], "roles": [],
              "external_sessions_unmetered": 0, "complete": False,
              "scope": "All recorded AgentKit launches; external chats and unrecorded probes excluded."}
    conn = None
    try:
        conn = db.connect_readonly(root)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        providers: dict[str, dict] = {}
        roles: dict[str, dict] = {}
        conn.execute("BEGIN")
        rows = conn.execute("SELECT p.id,p.provider,p.status,p.purpose,t.role FROM processes p "
                            "LEFT JOIN tasks t ON t.id=p.task_id ORDER BY p.id")
        for row in rows:
            identifier, provider, status, purpose, task_role = row
            role = task_role if purpose == "worker" and task_role else purpose
            receipt = usage_receipts.read(root, identifier)
            usage = receipt["usage"] if receipt else {}
            stale = False
            if receipt:
                try:
                    source = usage_receipts.path_for(root, identifier, "events.jsonl")
                    stale = receipt.get("fingerprint") != usage_receipts.fingerprint(source)
                except FileNotFoundError:
                    pass  # A retired log does not erase the durable receipt.
                except (OSError, ValueError, RuntimeError):
                    stale = True
            if status in ("RUNNING", "STARTING") or not receipt or stale:
                current = (live_usages or {}).get(identifier)
                if current is None:
                    current = read_details(root, identifier)
                if any(current.get(key) is not None for key in FIELDS):
                    usage = current
                elif stale:
                    usage = {**usage, "complete": False}
            _add(result, usage, provider)
            _add(providers.setdefault(provider, {"provider": provider, **_empty()}), usage, provider)
            _add(roles.setdefault(role, {"role": role, **_empty()}), usage, provider)
        result["providers"] = list(providers.values())
        result["roles"] = list(roles.values())
        if "manager_leases" in tables:
            result["external_sessions_unmetered"] = conn.execute(
                "SELECT COUNT(DISTINCT session_ref) FROM manager_leases WHERE session_ref IS NOT NULL AND session_ref<>''"
            ).fetchone()[0]
        result["complete"] = (result["launches"] > 0 and
                              result["complete_launches"] == result["launches"] and
                              not result["external_sessions_unmetered"])
    except (OSError, ValueError, sqlite3.Error):
        result["status"] = "unavailable"
    finally:
        if conn is not None:
            conn.close()
    return result


def command(args) -> int:
    from .paths import find_project_root
    root = find_project_root(args.path or Path.cwd())
    if root is None:
        raise ValueError("usage requires an initialized project")
    if args.backfill:
        conn = db.connect_readonly(root)
        try:
            usage_receipts.capture_stopped(root, conn, limit=1_000_000)
        finally:
            conn.close()
    result = snapshot(root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


def configure(sub) -> None:
    parser = sub.add_parser("usage", help="project-wide recorded tokens; never calls a model")
    parser.add_argument("--backfill", action="store_true", help="save receipts from stopped historical logs")
    parser.set_defaults(func=command)
