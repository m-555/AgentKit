"""Persistent process ownership shared by workers, reviewers and the coordinator."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import db


def get(conn, identifier: int) -> dict | None:
    row = conn.execute("SELECT * FROM processes WHERE id=?", (identifier,)).fetchone()
    return dict(row) if row else None


def active(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM processes WHERE status IN ('STARTING','RUNNING')")]


def owning(conn) -> list[dict]:
    """Status alone never proves the owning monitor and child have exited."""
    from .process_identity import alive
    from .reconcile import pid_alive
    return [dict(row) for row in conn.execute("SELECT * FROM processes")
            if row["status"] in ("STARTING", "RUNNING")
            or alive(dict(row), "pid", probe=pid_alive) or alive(dict(row), "child_pid", probe=pid_alive) or ownership_uncertain(row)]


def ownership_uncertain(record) -> bool:
    if record["child_launch_state"] == "WSL_UNCONFIRMED":
        return True
    if record["child_launch_state"] == "NOT_STARTED":
        return False
    attempted = bool(record["pid"] or record["session_token"] or record["child_launch_state"] in ("SPAWNING", "STARTED"))
    return attempted and (not record["pid"] or not record["child_pid"]) and (
        record["exit_code"] is None or not record["ended_at"])


def update(conn, identifier, **fields):
    from .process_identity import fingerprint
    for field in ("pid", "child_pid"):
        if fields.get(field):
            value = fingerprint(fields[field])
            fields[field + "_identity"] = json.dumps(value) if value else None
    conn.execute("UPDATE processes SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                 (*fields.values(), identifier))


def start(conn, root, launch, *, purpose: str, provider: str, task_id=None,
          job_id=None, generation=0, worker_run_id=None, expected_head=None) -> int:
    from .secrets import worker_environment
    with db.immediate_transaction(conn):
        if purpose == "coordinator" and job_id:
            from .manager import blocks_spawn
            if blocks_spawn(conn, job_id):
                raise ValueError("external manager lease or bridge process still owns this job")
        owners = owning(conn)
        if any((task_id is not None and p["task_id"] == task_id) or
               (purpose == "coordinator" and p["purpose"] == purpose and p["job_id"] == job_id)
               for p in owners):
            raise ValueError("a live monitor or child still owns this task or coordinator job")
        from . import recovery_runtime
        recovery_claim = recovery_runtime.before_launch(conn, root, purpose=purpose, provider=provider, task_id=task_id, job_id=job_id)
        payload = {"argv": launch.argv, "cwd": launch.cwd, "env": launch.env, "stdin_text": launch.stdin_text}
        env = launch.env or {}
        try:
            cursor = conn.execute("INSERT INTO processes(purpose,provider,task_id,job_id,generation,worker_run_id,expected_head,launch_json,started_at,"
                                  "requested_model,requested_effort,requested_profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (purpose, provider, task_id, job_id, generation, worker_run_id, expected_head,
                 json.dumps(payload), db.utcnow(), env.get("AGENTKIT_MODEL") or None,
                 env.get("AGENTKIT_MODEL_EFFORT") or None, env.get("AGENTKIT_MODEL_PROFILE") or None))
        except sqlite3.IntegrityError as exc:
            raise ValueError("a session already owns this task or coordinator job") from exc
        identifier = cursor.lastrowid
        recovery_runtime.launched(conn, identifier, recovery_claim)
    try:
        diagnostic = Path(root) / ".ai/runtime" / f"process-{identifier}" / "monitor.stderr.log"
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        with diagnostic.open("ab") as monitor_error:
            proc = subprocess.Popen([sys.executable, "-m", "agentkit.runner", str(Path(root).resolve()), str(identifier)],
                cwd=Path(__file__).resolve().parents[1], env=worker_environment(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=monitor_error,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
        update(conn, identifier, pid=proc.pid)
        try:
            from . import windows
            windows.spawn(root, int(identifier))
        except (OSError, ValueError) as exc:
            db.log_event(conn, task_id, "process_viewer_unavailable", cause=str(exc),
                         detail={"process": identifier})
    except OSError as exc:
        update(conn, identifier, status="FAILED", error=str(exc), ended_at=db.utcnow(), child_launch_state="NOT_STARTED", exit_code=127)
        raise
    return int(identifier)


def require_control(conn, *, purposes: tuple[str, ...], job_id=None, task_id=None) -> dict:
    """Only the supervisor's currently live control session can exercise authority."""
    identifier = os.environ.get("AGENTKIT_PROCESS")
    if not identifier:
        raise PermissionError("this operation requires a supervised coordinator or reviewer session")
    process = get(conn, int(identifier))
    if not process or process["status"] != "RUNNING" or process["purpose"] not in purposes:
        raise PermissionError(f"control session {identifier} is inactive or lacks authority: "
                              f"{process['status'] if process else 'missing'} / {process['purpose'] if process else 'missing'}")
    if job_id and process["job_id"] != job_id:
        raise PermissionError("control session belongs to another job")
    if task_id and process["purpose"] == "review" and process["task_id"] != task_id:
        raise PermissionError("reviewer belongs to another task")
    return process
