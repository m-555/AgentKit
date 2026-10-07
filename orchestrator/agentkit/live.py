"""Bounded, read-only agent visibility; stream contents are never rendered."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .reconcile import pid_alive
from .secrets import redact_text

MAX_ROWS = 200
MAX_BYTES = 65_536
MAX_LAUNCH_BYTES = 262_144
MAX_ACTIVITY = 6
_IDENTIFIER = re.compile(r"[\w./:@+-]{1,120}\Z", re.ASCII)
_SESSION = re.compile(r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|"
                      r"ses_[A-Za-z0-9]{8,100})\Z")


def _text(value: Any, limit: int = 200) -> str:
    # Strip terminal controls, including escapes, before rendering metadata.
    if not isinstance(value, (str, int, float, bool, type(None))):
        return "unknown"
    return "".join(c for c in redact_text(str(value or "")) if c.isprintable())[:limit]


def _label(value: Any) -> str:
    text = _text(value, 121)
    return text if _IDENTIFIER.fullmatch(text) else "unknown"


def _stamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).isoformat()
    except ValueError:
        return None


def _object(value: Any) -> dict:
    if not isinstance(value, str):
        return value if isinstance(value, dict) else {}
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _activity(message: dict) -> list[dict]:
    """Project vendor or normalized events to tool names/lifecycle only."""
    kind = message.get("kind") or message.get("type")
    if not isinstance(kind, str):
        return []
    if kind in ("tool_use", "tool_started", "tool_finished"):
        part = _object(message.get("part"))
        detail = _object(message.get("detail"))
        return [{"kind": "tool", "name": _label(message.get("tool_name") or
                 detail.get("tool_name") or detail.get("tool") or part.get("tool") or
                 message.get("name") or "tool"), "state": _label(kind)}]
    if kind == "assistant":
        content = _object(message.get("message")).get("content")
        if isinstance(content, list):
            return [{"kind": "tool", "name": _label(block.get("name")), "state": "started"}
                    for block in content[:100] if isinstance(block, dict) and
                    block.get("type") == "tool_use"]
    if kind in ("item.started", "item.updated", "item.completed"):
        item = _object(message.get("item"))
        names = {"command_execution": "command", "file_change": "file_change",
                 "mcp_tool_call": "mcp", "web_search": "web_search"}
        if isinstance(item.get("type"), str) and item["type"] in names:
            name = item.get("tool") if item.get("type") == "mcp_tool_call" else None
            return [{"kind": "tool", "name": _label(name or names[item["type"]]),
                     "state": str(kind).split(".")[-1]}]
    lifecycle = {"thread.started": "started", "started": "started", "finished": "finished",
                 "turn.completed": "finished", "result": "finished", "error": "error",
                 "turn.failed": "error", "quota": "quota", "rate_limit_event": "quota"}
    if kind in lifecycle:
        return [{"kind": lifecycle[kind]}]
    if kind == "system" and message.get("subtype") == "init":
        return [{"kind": "started"}]
    return []


def _stream(root: Path, identifier: int) -> dict:
    """Read a bounded tail at a derived path, refusing escaping symlinks."""
    result: dict = {"activity": [], "status": "missing"}
    path = root / ".ai" / "runtime" / f"process-{identifier}" / "events.jsonl"
    try:
        path.resolve().relative_to((root / ".ai" / "runtime").resolve())
        # A substituted runtime directory must not grant access outside the project.
        path.resolve().relative_to(root.resolve())
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            offset = max(0, size - MAX_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_BYTES)
    except (OSError, ValueError, RuntimeError):
        return result
    lines = data.splitlines(keepends=True)
    if offset:
        lines = lines[1:]  # May be the suffix of an oversized record.
    malformed = 0
    for line in lines[-200:]:
        if not line.endswith(b"\n"):
            malformed += 1  # A writer may still be appending this record.
            continue
        record = _object(line.decode("utf-8", errors="replace"))
        if not record:
            malformed += 1
            continue
        if record.get("channel") == "stderr":
            continue
        message = _object(record.get("text")) if "channel" in record else record
        for event in _activity(message):
            result["activity"].append({"at": _stamp(record.get("at")), **event})
    result["activity"] = result["activity"][-MAX_ACTIVITY:]
    result.update(status="ok", partial=bool(offset), skipped_records=malformed)
    return result


def _rows(conn: sqlite3.Connection, table: str, columns: str, order: str = "",
          params: tuple = ()) -> list[dict]:
    # Table names, selected columns and ordering are exclusively local constants.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone():
        return []
    return [dict(row) for row in conn.execute(
        f"SELECT {columns} FROM {table} {order} LIMIT {MAX_ROWS}", params)]


def _task(row: dict, gates: list[dict], events: list[dict]) -> dict:
    meta = _object(row.pop("blocked_meta", None))
    row = {key: _text(value) if isinstance(value, str) else value for key, value in row.items()}
    status = row.get("status")
    row["progress"] = ("WAITING_QUOTA" if status == "BLOCKED" and
                       meta.get("reason") == "provider_usage_limit" else
                       "WAITING" if status in ("READY", "PLANNED", "BLOCKED", "NEEDS_REPLAN")
                       else status)
    row["retry_at"] = _stamp(meta.get("retry_at"))
    row["tests"] = next(({"level": _label(g["level"]), "passed": bool(g["passed"]),
                          "head_sha": _label(g["head_sha"]), "at": _stamp(g["created_at"])}
                         for g in gates if g["task_id"] == row["id"]), None)
    row["last_event"] = next((_label(e["kind"]) for e in events
                              if e["task_id"] == row["id"]), None)
    return row


def _process(root: Path, row: dict, tasks: dict) -> dict:
    from .processes import ownership_uncertain
    row["ownership_uncertain"] = ownership_uncertain(row)
    launch = _object(row.pop("launch_json", None))
    env = _object(launch.get("env"))
    task = tasks.get(row.get("task_id"), {})
    row = {key: _text(value) if isinstance(value, str) else value for key, value in row.items()}
    session = row.pop("session_token", None)
    row["session_id"] = session if session and _SESSION.fullmatch(session) else "unknown"
    row["requested_model"] = _label(row.get("requested_model") or env.get("AGENTKIT_MODEL") or
                                    task.get("model"))
    row["requested_effort"] = _label(row.get("requested_effort") or
                                     env.get("AGENTKIT_MODEL_EFFORT") or
                                     ("local-default" if row["provider"] == "local-opencode" else None))
    row["observed_model"], row["observed_effort"] = (_label(row.get("observed_model")),
                                                    _label(row.get("observed_effort")))
    row["model_verified"] = row.get("model_verified") == 1
    row.update(model=row["observed_model"] if row["model_verified"] else row["requested_model"],
               effort=row["observed_effort"] if row["model_verified"] else row["requested_effort"],
               model_source="observed" if row["model_verified"] else "requested",
               worktree=_text(launch.get("cwd") or task.get("worktree")),
               role=_label(task.get("role") if row["purpose"] == "worker" else row["purpose"]))
    for key in ("pid", "child_pid"):
        if not isinstance(row.get(key), int) or row[key] <= 0:
            row[key] = None
    state = row["status"]
    from .process_identity import alive
    monitor = alive(row, "pid", probe=pid_alive) if row["pid"] else None
    child = alive(row, "child_pid", probe=pid_alive) if row["child_pid"] else None
    row.pop("pid_identity", None)
    row.pop("child_pid_identity", None)
    row["monitor_alive"], row["child_alive"] = monitor, child
    row["liveness_risk"] = state not in ("STARTING", "RUNNING") and bool(monitor or child)
    if state in ("STARTING", "RUNNING"):
        if row["pid"] and not monitor:
            state = "ORPHANED" if child else "CRASHED"
    elif monitor or child:
        state = "TERMINAL_PID_ALIVE"
    elif state == "FINISHED":
        state = "EXIT_UNCONFIRMED" if monitor is None and child is None else "STOPPED"
    row.update(state=state, progress=task.get("progress"),
               stream=_stream(root, int(row["id"])))
    return row


def _manager(row: dict, states: list[dict]) -> dict:
    row = {key: _text(value) if isinstance(value, str) else value for key, value in row.items()}
    session = row.pop("session_ref", None)
    row["session_id"] = session if session and _SESSION.fullmatch(session) else "unknown"
    row["model_verified"] = False  # External registration reports metadata, not provider proof.
    heartbeat = _stamp(row.get("heartbeat_at"))
    pid = row.get("pid")
    row["pid"] = pid if isinstance(pid, int) and pid > 0 else None
    expired = (not heartbeat or not isinstance(row.get("ttl_seconds"), int) or
               (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds() >
               row["ttl_seconds"])
    row["state"] = ("RELEASED" if row.get("released_at") else
                    "CRASHED" if row["pid"] and not pid_alive(row["pid"]) else
                    "STALE" if expired else "ACTIVE")
    row["recovery"] = next(({k: _text(v) if isinstance(v, str) else v for k, v in state.items()}
                            for state in states if state["job_id"] == row["job_id"]), None)
    return row


def snapshot(root: str | Path, *, task_id: int | None = None,
             process_id: int | None = None) -> dict:
    """Read existing state only; never call the schema-creating db.connect."""
    root = Path(root)
    view: dict = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "status": "ok",
                  "tasks": [], "processes": [], "external_managers": [],
                  "providers": [], "quota_windows": []}
    path = root / ".ai" / "tasks.db"
    if not (root / ".ai" / "project.yaml").is_file() or not path.is_file():
        view.update(status="missing", message="No AgentKit project/runtime database recorded yet.")
        return view
    conn = None
    try:
        path.resolve().relative_to(root.resolve())
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        params = (task_id,) if task_id is not None else ()
        filtered = "WHERE id=? " if params else ""
        task_filtered = "WHERE task_id=? " if params else ""
        task_columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        blocked = "blocked_meta" if "blocked_meta" in task_columns else "NULL AS blocked_meta"
        tasks = _rows(conn, "tasks", "id,spec_id,title,status,role,generation,adapter,model,"
                      "worktree,branch,heartbeat_at," + blocked, filtered + "ORDER BY id DESC",
                      params)
        gates = _rows(conn, "gate_results", "task_id,level,passed,head_sha,created_at",
                      task_filtered + "ORDER BY id DESC", params)
        events = _rows(conn, "events", "task_id,kind", task_filtered + "ORDER BY id DESC", params)
        present = {r[1] for r in conn.execute("PRAGMA table_info(processes)")}
        optional = ",".join(name if name in present else f"NULL AS {name}" for name in
                            ("requested_model", "requested_effort", "requested_profile",
                             "observed_model", "observed_effort", "model_verified", "child_launch_state",
                             "pid_identity", "child_pid_identity"))
        process_params = (process_id,) if process_id is not None else params
        process_filter = ("WHERE id=? " if process_id is not None else
                          "WHERE task_id=? OR task_id IS NULL " if params else "")
        rows = _rows(conn, "processes", "id,purpose,task_id,job_id,provider,account,generation,"
                     "status,pid,child_pid,session_token,started_at,heartbeat_at,ended_at,exit_code,"
                     f"substr(launch_json,1,{MAX_LAUNCH_BYTES}) AS launch_json," + optional,
                     process_filter + "ORDER BY status IN ('RUNNING','STARTING') DESC,id DESC",
                     process_params)
        view["providers"] = _rows(conn, "provider_state", "account_key,provider,account,status,"
                                   "retry_at,detected_at", "ORDER BY account_key")
        view["quota_windows"] = _rows(conn, "quota_windows", "account_key,bucket,window,"
                                       "used_percent,resets_at,observed_at", "ORDER BY account_key")
        managers = _rows(conn, "manager_leases", "job_id,holder,provider,model,effort,session_ref,"
                          "pid,ttl_seconds,takeover_grace,acquired_at,heartbeat_at,released_at,"
                          "outage_recorded", "ORDER BY heartbeat_at DESC")
        states = _rows(conn, "manager_state", "job_id,epoch,acknowledged_epoch,audit_epoch,"
                        "audit_at,ack_at,updated_at", "ORDER BY updated_at DESC")
        conn.rollback()
        view["external_managers"] = [_manager(manager, states) for manager in managers]
        by_id = {t["id"]: _task(t, gates, events) for t in tasks}
        view["tasks"] = [t for t in by_id.values() if task_id is None or t["id"] == task_id]
        view["processes"] = [_process(root, p, by_id) for p in rows
                             if task_id is None or p["task_id"] in (None, task_id)]
        if task_id is not None and not view["tasks"]:
            view.update(status="missing_task", message=f"Task {task_id} is not recorded.")
        for table in ("providers", "quota_windows"):
            view[table] = [{k: _stamp(v) if k.endswith("_at") else
                           _label(v) if isinstance(v, str) else v for k, v in r.items()}
                           for r in view[table]]
        view["bounded"] = any(len(group) == MAX_ROWS for group in
                              (tasks, rows, gates, events, managers, states))
    except (OSError, ValueError, sqlite3.Error):
        view.update(status="unavailable", message="Runtime state is busy, unreadable or incompatible.")
    finally:
        if conn is not None:
            conn.close()
    return view


def render(view: dict) -> str:
    """Plain terminal frames also work in redirected logs and narrow consoles."""
    lines = [f"AGENTKIT LIVE  {view['at']}"]
    if view.get("message"):
        lines.append(view["message"])
    lines.append("PROCESSES")
    if not view["processes"]:
        lines.append("  No agent processes recorded.")
    for row in view["processes"]:
        lines.append(f"  #{row['id']} {row['role']} {row['provider']}  {row['state']} "
                     f"(recorded {row['status']})  task={row['task_id']} job={row['job_id'] or '-'}")
        lines.append(f"    model={row['model']} effort={row['effort']} "
                     f"source={row['model_source']} verified={row['model_verified']} "
                     f"pid={row['pid'] or '-'} child={row['child_pid'] or '-'} "
                     f"session={row['session_id']}")
        lines.append(f"    requested={row['requested_model']}/{row['requested_effort']} "
                     f"observed={row['observed_model']}/{row['observed_effort']}")
        lines.append(f"    monitor_alive={row['monitor_alive']} child_alive={row['child_alive']} "
                     f"liveness_risk={row['liveness_risk']} "
                     f"ownership_uncertain={row['ownership_uncertain']}")
        lines.append(f"    worktree={row['worktree'] or '-'} heartbeat={row['heartbeat_at'] or '-'}")
        for event in row["stream"]["activity"]:
            lines.append(f"    {event['at'] or '-'} {event['kind']} "
                         f"{event.get('name', '')} {event.get('state', '')}".rstrip())
        if row["stream"]["status"] == "missing":
            lines.append("    stream not recorded or inaccessible")
    lines.append("EXTERNAL MANAGERS (reported metadata)")
    if not view["external_managers"]:
        lines.append("  No external manager registrations recorded.")
    for manager in view["external_managers"]:
        lines.append(f"  {manager['holder']} job={manager['job_id']} {manager['state']} "
                     f"{manager['provider']} model={manager['model']} effort={manager['effort']} "
                     f"pid={manager['pid'] or '-'} session={manager['session_id']}")
        lines.append(f"    heartbeat={manager['heartbeat_at']} ttl={manager['ttl_seconds']}s")
        recovery = manager["recovery"]
        if recovery:
            lines.append(f"    recovery_epoch={recovery['epoch']} acknowledged="
                         f"{recovery['acknowledged_epoch']} audit_epoch={recovery['audit_epoch']}")
    lines.append("TASKS")
    if not view["tasks"]:
        lines.append("  No tasks recorded.")
    for task in view["tasks"]:
        gate = task["tests"]
        tests = (f"{gate['level']}:{'PASS' if gate['passed'] else 'FAIL'}@{gate['head_sha'][:12]}"
                 if gate else "not recorded")
        lines.append(f"  #{task['id']} {task['title']}  {task['status']} / {task['progress']} "
                     f"tests={tests} last_event={task['last_event'] or '-'}")
        if task["progress"] == "WAITING_QUOTA":
            lines.append(f"    next availability check={task['retry_at'] or 'unknown'}")
    lines.append("PROVIDER QUOTA (saved observations; reset permits a check)")
    for state in view["providers"]:
        lines.append(f"  {state['account_key']} {state['status']} "
                     f"next_check={state['retry_at'] or 'unknown'}")
    for window in view["quota_windows"]:
        lines.append(f"  {window['account_key']} {window['bucket']}/{window['window']} "
                     f"used={window['used_percent']}% reset={window['resets_at'] or 'unknown'} "
                     f"observed={window['observed_at']}")
    if not view["providers"] and not view["quota_windows"]:
        lines.append("  No account quota observations recorded.")
    if view.get("bounded"):
        lines.append(f"  Showing at most {MAX_ROWS} recent records per table.")
    return "\n".join(lines)


def run(root: str | Path, *, task_id: int | None = None, follow: bool = False,
        poll_seconds: float = 2.0, json_output: bool = False, max_iterations: int | None = None,
        sleeper: Callable[[float], Any] = time.sleep,
        output: Callable[[str], Any] = print) -> int:
    """Print snapshots until interrupted. Each iteration discovers new processes."""
    if (not math.isfinite(poll_seconds) or poll_seconds < 0.1 or
            (max_iterations is not None and max_iterations < 1)):
        output("poll must be finite and at least 0.1 seconds; max_iterations must be positive")
        return 2
    count = 0
    try:
        while True:
            view = snapshot(root, task_id=task_id)
            output(json.dumps(view, ensure_ascii=True) if json_output else render(view))
            count += 1
            if not follow or (max_iterations is not None and count >= max_iterations):
                return 0 if view["status"] == "ok" else 1
            sleeper(poll_seconds)
    except KeyboardInterrupt:
        return 0
