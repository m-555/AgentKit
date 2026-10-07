"""Making autonomy explainable (PLAN_V3 §15).

An autonomous system that cannot answer "why did you do that?" cannot be trusted
or debugged. Four questions must be answerable from the event log alone, and each
maps to a single query here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db, providers, quota
from . import statemachine as sm

COLUMNS = (
    ("TASK", 5), ("KIND", 15), ("STATUS", 18), ("AGENT", 12), ("MODEL", 9),
    ("BRANCH", 26), ("LEASE", 26), ("TESTS", 11), ("$", 6), ("RETRY", 5),
)


def status_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    leases_by_task: dict[int, list[str]] = {}
    for lease in db.active_leases(conn):
        leases_by_task.setdefault(int(lease["task_id"]), []).append(str(lease["path_glob"]))

    rows = []
    for task in db.list_tasks(conn):
        task_id = int(task["id"])
        gate = conn.execute(
            "SELECT level, passed FROM gate_results WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        last_event = db.recent_events(conn, task_id, limit=1)
        leases = leases_by_task.get(task_id, [])
        rows.append({
            "id": task_id,
            "spec_id": task.get("spec_id"),
            "kind": task.get("kind"),
            "status": task.get("status"),
            "agent": task.get("adapter") or "-",
            "model": task.get("model") or "-",
            "branch": task.get("branch") or "-",
            "worktree": task.get("worktree") or "-",
            "lease": ", ".join(leases) if leases else "-",
            "tests": (
                f"{gate['level']}:{'PASS' if gate['passed'] else 'FAIL'}" if gate else "-"
            ),
            "spend": float(task.get("spend_usd") or 0.0),
            "budget": task.get("budget_usd"),
            "retries": int(task.get("attempts") or 0),
            "depends_on": task.get("depends_on") or [],
            "blocker": task.get("blocker") or "",
            "last_event": last_event[0]["kind"] if last_event else "-",
            "last_commit": task.get("last_commit") or "-",
            "title": task.get("title"),
            **_quota_fields(task),
        })
    return rows


def render_providers(conn: sqlite3.Connection) -> str:
    """Why something is waiting, answerable at a glance after a night away."""
    states = providers.list_states(conn)
    if not states:
        return "PROVIDERS\n  (none recorded yet)"
    lines = ["PROVIDERS"]
    for state in sorted(states, key=lambda s: s.provider):
        if state.available_at():
            lines.append(f"  {state.provider:<10} AVAILABLE")
            continue
        when = db.parse_ts(state.retry_at)
        stamp = when.astimezone().strftime("%H:%M") if when else "unknown"
        minutes = max(1, state.seconds_remaining() // 60)
        lines.append(
            f"  {state.provider:<10} {state.status:<10} resets {stamp} "
            f"(~{minutes} min) - {state.reason}"
        )
    return "\n".join(lines)


def _quota_fields(task: dict[str, Any]) -> dict[str, Any]:
    if not quota.is_quota_paused(task):
        return {}
    meta = quota.blocked_meta(task)
    when = db.parse_ts(meta.get("retry_at"))
    return {
        "waiting_on": meta.get("provider", "provider"),
        "resumes_at": when.astimezone().strftime("%H:%M") if when else "when available",
    }


def render_status(conn: sqlite3.Connection) -> str:
    rows = status_rows(conn)
    if not rows:
        return render_providers(conn) + "\n\nNo tasks. Write .ai/tasks.yaml and run `agentkit reconcile`."

    lines = [render_providers(conn), "", "TASKS"]
    header = "  ".join(name.ljust(width) for name, width in COLUMNS)
    lines += [header, "-" * len(header)]
    for row in rows:
        cells = [
            str(row["id"]).ljust(5),
            str(row["kind"])[:15].ljust(15),
            str(row["status"])[:18].ljust(18),
            str(row["agent"])[:12].ljust(12),
            str(row["model"])[:9].ljust(9),
            str(row["branch"])[-26:].ljust(26),
            str(row["lease"])[:26].ljust(26),
            str(row["tests"])[:11].ljust(11),
            f"{row['spend']:.2f}".ljust(6),
            str(row["retries"]).ljust(5),
        ]
        lines.append("  ".join(cells))
        if row.get("resumes_at"):
            lines.append(
                f"       waiting on {row['waiting_on']} usage limit; "
                f"resumes {row['resumes_at']}"
            )
        elif row["blocker"]:
            lines.append(f"       blocked: {row['blocker']}")

    paused = [r for r in rows if r.get("resumes_at")]
    ready = [r for r in rows if r["status"] == sm.READY]
    running = [r for r in rows if r["status"] in (sm.LEASED, sm.RUNNING, sm.VERIFYING)]
    stale = [r for r in rows if r["status"] == sm.STALE]
    replan = [r for r in rows if r["status"] == sm.NEEDS_REPLAN]

    lines.append("")
    lines.append(
        f"{len(running)} running | {len(ready)} ready | {len(paused)} waiting on quota | "
        f"{len(stale)} stale | {len(replan)} need replanning"
    )
    if stale:
        lines.append(
            "  stale tasks keep their worktree; `agentkit adopt <id>` or "
            "`agentkit discard <id>` to resolve"
        )
    amendments = db.list_amendments(conn)
    if amendments:
        lines.append(f"\n{len(amendments)} open amendment(s) waiting on you:")
        for amendment in amendments:
            lines.append(f"  #{amendment['id']} (task {amendment['task_id']}): "
                         f"{amendment['proposal']}")
    return "\n".join(lines)


# ------------------------------------------------------------ the why queries


def why_launched(conn: sqlite3.Connection, task_id: int) -> str:
    events = db.recent_events(conn, task_id, kind="worker_launched", limit=1)
    if not events:
        return f"Task {task_id} has not been launched."
    event = events[0]
    detail = event["detail"]
    lines = [
        f"Task {task_id} launched at {event['at']}",
        f"  reason     : {event['cause']}",
        f"  adapter    : {detail.get('adapter')} (generation {detail.get('generation')})",
        f"  worktree   : {detail.get('worktree')}",
        f"  base commit: {detail.get('base_sha')}",
        f"  lease      : {', '.join(detail.get('lease_paths') or []) or '-'}",
        f"  guards live: {', '.join(detail.get('guards') or []) or 'none'}",
    ]
    return "\n".join(lines)


def why_stopped(conn: sqlite3.Connection, task_id: int) -> str:
    terminal = (
        "recovery_decision", "lease_expired", "merge_rejected", "gate_run",
        "status_changed", "worker_exited",
    )
    for event in db.recent_events(conn, task_id, limit=50):
        if event["kind"] in terminal:
            return (
                f"Task {task_id} — last decisive event at {event['at']}\n"
                f"  event : {event['kind']}\n"
                f"  cause : {event['cause'] or '-'}\n"
                f"  effect: {event['effect'] or '-'}"
            )
    return f"No stop event recorded for task {task_id}."


def why_serialized(conn: sqlite3.Connection, task_id: int) -> str:
    events = db.recent_events(conn, task_id, kind="serialization_decision", limit=3)
    if not events:
        return f"Task {task_id} has not been serialised behind anything."
    lines = [f"Task {task_id} serialisation decisions:"]
    for event in events:
        detail = event["detail"]
        lines.append(f"  at {event['at']}: {event['cause']}")
        lines.append(f"    effect     : {event['effect']}")
        lines.append(f"    overlapping: {', '.join(detail.get('overlapping') or []) or '-'}")
        left = detail.get("left") or {}
        if left.get("sources"):
            lines.append(f"    predicted from: {', '.join(left['sources'])}"
                         f" (confidence {left.get('confidence')})")
    return "\n".join(lines)


def why_merge_failed(conn: sqlite3.Connection, task_id: int) -> str:
    events = db.recent_events(conn, task_id, kind="merge_rejected", limit=1)
    if not events:
        return f"No merge rejection recorded for task {task_id}."
    event = events[0]
    detail = event["detail"]
    lines = [
        f"Task {task_id} merge rejected at {event['at']}",
        f"  cause : {event['cause']}",
        f"  effect: {event['effect']}",
    ]
    for violation in detail.get("violations") or []:
        lines.append(f"    out of lease: {violation.get('path')}")
    for conflict in detail.get("conflicts") or []:
        lines.append(f"    conflict: {conflict}")
    gate = detail.get("gate") or {}
    for command in gate.get("commands") or []:
        if not command.get("ok"):
            lines.append(f"    failing command: {command.get('command')}")
    return "\n".join(lines)


def render_events(conn: sqlite3.Connection, task_id: int | None, limit: int) -> str:
    events = db.recent_events(conn, task_id, limit=limit)
    if not events:
        return "No events recorded."
    lines = []
    for event in reversed(events):
        target = f"task {event['task_id']}" if event["task_id"] else "system"
        lines.append(f"{event['at']}  {target:<10} {event['kind']}")
        if event["cause"]:
            lines.append(f"    cause : {event['cause']}")
        if event["effect"]:
            lines.append(f"    effect: {event['effect']}")
    return "\n".join(lines)
