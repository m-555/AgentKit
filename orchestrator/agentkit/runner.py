"""One restart-independent process monitor per agent session.

The monitor survives a CLI/watch restart. It records events, session IDs, exit
codes and checkpoints, and continues heartbeats even while an agent is silent.
"""
from __future__ import annotations

import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

from . import (
    adapters,
    audit,
    checkpoints,
    db,
    gates,
    processes,
    providers,
    quota,
    recovery,
    repo,
    sessions,
)
from .config import load_project
from .secrets import redact_text, worker_environment

#: Waits between attempts at an essential monitor write while SQLite is busy.
#: Each attempt already waits `db.BUSY_TIMEOUT_MS` for the lock itself.
BUSY_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)


def _busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def _essential(action):
    """Retry a write the monitor cannot skip, through transient lock contention."""
    for delay in (*BUSY_RETRY_DELAYS, None):
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if delay is None or not _busy(exc):
                raise
            time.sleep(delay)


def _section_fault(exc: Exception, degraded: str, section: str, *, repeated: bool = False) -> str:
    """Record a monitor-side fault without touching the worker.

    A busy database only skips a `repeated` section: the heartbeat runs again
    within 15 seconds and the completion audit repeats its checks. Any other
    fault, or a skipped event whose model and session checks never repeat, marks
    supervision as degraded, so the finished task is held for the manager
    instead of being judged on checks that did not run.
    """
    if repeated and _busy(exc):
        sys.stderr.write(f"[AgentKit monitor] {section} skipped, database busy: {exc}\n")
        return degraded
    sys.stderr.write(f"[AgentKit monitor] {section} failed; worker left running: {exc}\n")
    return degraded or f"monitor {section} error: {exc}"


def _hold_for_host_fault(conn, root, process, code, reason):
    """Preserve a worker's output after a monitor fault and hold it for the manager.

    The fault is AgentKit's, so the task keeps its attempts and the run keeps the
    worker's real exit code.
    """
    if process.get("worker_run_id"):
        db.close_worker_run(conn, process["worker_run_id"], exit_code=code)
    task = db.get_task(conn, process["task_id"])
    if not task or task["generation"] != process["generation"]:
        return
    if task.get("worktree"):
        checkpoints.write_mechanical(conn, root, task["worktree"], task["id"], "host_monitor_fault")
    from . import statemachine as sm
    if sm.can(task["status"], "BLOCKED", "scheduler"):
        db.update_task(conn, task["id"], blocker=reason, next_action=(
            "AgentKit monitor fault; the worker was not judged on missed checks and its output "
            "is preserved. Manager inspects the checkpoint, then completes or requeues it."))
        db.set_status(conn, task["id"], "BLOCKED", cause=reason)


def finish_worker(conn, root, process, code, failure_text):
    task_id = process["task_id"]
    task = db.get_task(conn, task_id)
    if not task or task["generation"] != process["generation"]:
        # A superseded run still ended; its bookkeeping must say so, or a later
        # continuation could never prove the previous worker finished.
        if process.get("worker_run_id"):
            db.close_worker_run(conn, process["worker_run_id"], exit_code=code)
        return
    from . import transport_ownership
    if transport_ownership.is_wsl(process) and not transport_ownership.exit_confirmed(root, process["id"]):
        processes.update(conn, process["id"], child_launch_state="WSL_UNCONFIRMED")
        db.update_task(conn, task_id, blocker="Linux worker termination is unconfirmed; ownership preserved")
        return
    work = task.get("worktree")
    if not work:
        _retry(conn, task, "Worker has no recorded worktree")
        return
    if work:
        checkpoints.write_mechanical(conn, root, work, task_id, "worker_exit")
    if process.get("worker_run_id"):
        db.close_worker_run(conn, process["worker_run_id"], exit_code=code)
    if task["status"] in ("DONE", "CANCELLED", "NEEDS_REPLAN", "INTEGRATION_READY"):
        return
    if task["status"] == "BLOCKED" and not quota.is_quota_paused(task):
        return
    if failure_text.startswith("[AgentKit execution limit]"):
        db.update_task(conn, task_id, blocker=failure_text, next_action="Manager must inspect checkpoint and split or explicitly requeue the task.")
        from . import statemachine as sm
        if sm.can(task["status"], "BLOCKED", "scheduler"):
            db.set_status(conn, task_id, "BLOCKED", cause=failure_text)
        # A late report must not crash supervision or undo review/stale/integration
        # state. Keep the limit visible for explicit manager recovery.
        return
    if failure_text or code:
        handled = quota.handle_worker_failure(conn, task_id, process["provider"], failure_text, code)
        if handled:
            return
        if "session" in failure_text.lower() and any(s in failure_text.lower() for s in ("not found", "invalid", "missing")):
            db.update_task(conn, task_id, session_token=None)
            action = recovery.decide(conn, task, recovery.Failure.PROVIDER_UNAVAILABLE)
        else:
            from .policy import record_trigger
            record_trigger(conn, task_id, "CRASH", process["provider"], str(task.get("model") or ""))
            from .workflow import enabled
            if enabled(load_project(root)):
                action = recovery.RecoveryAction(recovery.Failure.CRASH, "FAILED", True, False,
                    "Worker failure preserved; manager must inspect and authorize a fresh bounded retry.")
            else:
                action = recovery.decide(conn, task, recovery.Failure.CRASH, failure_text)
        recovery.apply(conn, task_id, action)
        return
    if task["status"] == "BLOCKED":
        return  # A concrete worker blocker requires manager repair, not retry.
    project = load_project(root)
    result = audit.audit_worktree(conn, project, work, task_id)
    if not result.clean:
        recovery.apply(conn, task_id, recovery.decide(conn, task, recovery.Failure.LEASE_VIOLATION))
        return
    from .worker_preparation import validate_completed
    try:
        validate_completed(project, task, work)
    except ValueError as error:
        _retry(conn, task, str(error))
        return
    if not repo.is_clean(work):
        # Preserve and return the task with explicit instructions, never discard dirt.
        _retry(conn, task, "Commit the preserved in-scope changes and rerun the gate.")
        return
    head = repo.head_commit(work)
    cached = db.cached_gate(conn, task_id, task["gate_level"], head)
    if cached and cached["passed"]:
        passed, summary = True, cached["summary"]
    else:
        gate = gates.run_gate(project, task["gate_level"], cwd=work)
        passed, summary = gate.passed, gate.summary()
        db.record_gate(conn, task_id, task["gate_level"], head,
                       passed and repo.is_clean(work) and repo.head_commit(work) == head, summary)
    if not passed or not repo.is_clean(work) or repo.head_commit(work) != head:
        _retry(conn, task, summary)
        return
    if task["status"] == "RUNNING":
        db.set_status(conn, task_id, "VERIFYING", cause="supervisor checked task gate")
    current = db.get_task(conn, task_id)
    assert current is not None
    if current["status"] == "VERIFYING":
        db.set_status(conn, task_id, "REVIEW", cause="clean committed work and gate passed")
        from .policy import clear_trigger
        clear_trigger(conn, task_id)


def _retry(conn, task, reason):
    from . import statemachine as sm
    current = db.get_task(conn, task["id"])
    assert current is not None
    if sm.can(current["status"], "FAILED"):
        db.set_status(conn, task["id"], "FAILED", cause=reason)
    db.update_task(conn, task["id"], attempts=int(current["attempts"]) + 1,
                   blocker=reason, next_action=reason)


def run(root: Path, identifier: int) -> int:
    conn = db.connect(root)
    process = processes.get(conn, identifier)
    if not process or process["status"] != "STARTING":
        conn.close()
        return 1
    launch = json.loads(process["launch_json"])
    env = worker_environment({**launch["env"], "AGENTKIT_ROOT": str(root),
                              "AGENTKIT_PROCESS": str(identifier), "AGENTKIT_ROLE": process["purpose"]})
    folder = root / ".ai" / "runtime" / f"process-{identifier}"
    folder.mkdir(parents=True, exist_ok=True)
    adapter = adapters.get(process["provider"])
    child = None
    failure = ""
    answers = []
    stderr_tail = ""
    code = 127
    from .run_limits import Meter, limits
    from .worker_stop import stop
    budget = limits(load_project(root)) if process["purpose"] == "worker" else {}
    meter = Meter()
    started = time.monotonic()
    limit_stop = ""
    limit_at = 0.0
    degraded = ""
    processes.update(conn, identifier, status="RUNNING", pid=os.getpid(), heartbeat_at=db.utcnow())
    try:
        if adapter is None:
            raise ValueError("worker adapter is unavailable")
        processes.update(conn, identifier, child_launch_state="SPAWNING")
        child = subprocess.Popen(launch["argv"], cwd=launch["cwd"], env=env,
            stdin=subprocess.PIPE if launch.get("stdin_text") is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt")
        processes.update(conn, identifier, child_pid=child.pid, child_launch_state="STARTED")
        messages: queue.Queue = queue.Queue()
        def read(stream, channel):
            for line in stream:
                messages.put((channel, line))
            messages.put((channel, None))
        readers = []
        for stream, channel in ((child.stdout, "stdout"), (child.stderr, "stderr")):
            thread = threading.Thread(target=read, args=(stream, channel), daemon=True)
            thread.start()
            readers.append((stream, thread))
        if child.stdin:
            def feed():
                pipe = child.stdin
                if pipe is None:
                    return
                try:
                    pipe.write(launch["stdin_text"])
                    pipe.close()
                except (BrokenPipeError, OSError):
                    pass  # Exit classification handles a worker that stopped reading.
            threading.Thread(target=feed, daemon=True).start()
        ended = 0
        last_heartbeat = 0.0
        with (folder / "events.jsonl").open("a", encoding="utf-8") as log:
            while ended < 2 or child.poll() is None:
                now = time.monotonic()
                exceeded = meter.exceeded(budget, now - started)
                if exceeded and not limit_stop and child.poll() is None:
                    limit_stop = "[AgentKit execution limit] " + exceeded
                    failure = limit_stop
                    limit_at = now
                    stop(child)
                if limit_stop and now - limit_at > 10:
                    stop(child, force=True)
                    if child.poll() is not None and now - limit_at > 12:
                        break  # Bounded drain even if an escaped descendant holds a pipe.
                if now - last_heartbeat > 15:
                    last_heartbeat = now
                    try:
                        processes.update(conn, identifier, heartbeat_at=db.utcnow())
                        if process["purpose"] == "worker" and not limit_stop:
                            from . import monitor_audit
                            detected = monitor_audit.check(conn, root, process, launch["cwd"])
                            if detected:
                                failure = limit_stop = detected
                                limit_at = now
                                stop(child)
                        if process["purpose"] == "worker":
                            task = db.get_task(conn, process["task_id"])
                            if task is None:
                                child.terminate()
                                failure = "worker fenced: its task was removed"
                            elif task["generation"] != process["generation"] or task["status"] in ("CANCELLED", "NEEDS_REPLAN"):
                                child.terminate()
                                failure = "worker fenced after cancellation or replan"
                            else:
                                db.heartbeat(conn, task["id"], process["generation"])
                    except Exception as exc:
                        degraded = _section_fault(exc, degraded, "heartbeat", repeated=True)
                try:
                    channel, line = messages.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    ended += 1
                    continue
                log.write(json.dumps({"at": db.utcnow(), "channel": channel, "text": redact_text(line)}) + "\n")
                log.flush()
                if channel == "stderr":
                    stderr_tail = (stderr_tail + line)[-12000:]
                    continue
                if budget:
                    meter.observe(line)
                try:
                    for event in adapter.parse_events(io.StringIO(line)):
                        if event.kind == "started":
                            from .recovery_runtime import turn_started
                            turn_started(conn, identifier)
                        if event.kind in ("started", "finished") and (event.detail.get("model") or event.detail.get("effort")):
                            process = processes.get(conn, identifier) or process
                            substituted = sessions.observe(conn, process, event.detail)
                            if substituted:
                                failure = (failure + substituted)[-12000:]
                                if child.poll() is None:
                                    child.terminate()
                        if event.kind == "started" and event.detail.get("session_id"):
                            token = event.detail["session_id"]
                            processes.update(conn, identifier, session_token=token)
                            if process["purpose"] == "worker":
                                db.update_task(conn, process["task_id"], session_token=token)
                            elif process["purpose"] == "coordinator":
                                conn.execute("UPDATE jobs SET coordinator_session=? WHERE id=?", (token, process["job_id"]))
                        elif event.kind == "quota" and event.detail.get("windows"):
                            providers.observe(conn, process["provider"], {**event.detail, "available": True, "reason": "worker quota event"})
                        elif event.kind == "error":
                            failure = (failure + event.raw)[-12000:]
                        elif event.kind == "answer":
                            answers.append(event.detail.get("text", ""))
                        elif event.kind == "finished":
                            if event.detail.get("is_error"):
                                failure = (failure + str(event.detail.get("result", "")))[-12000:]
                            elif event.detail.get("result"):
                                answers.append(str(event.detail["result"]))
                            if process["purpose"] == "worker" and event.detail.get("cost_usd"):
                                task = db.get_task(conn, process["task_id"])
                                assert task is not None
                                db.update_task(conn, task["id"], spend_usd=task["spend_usd"] + float(event.detail["cost_usd"]))
                except Exception as exc:
                    degraded = _section_fault(exc, degraded, "event")
        code = child.wait()
        if limit_stop:
            failure = limit_stop
        elif meter.stop_reason:
            failure = "[AgentKit execution limit] " + meter.stop_reason
        if code and not failure:
            failure = stderr_tail or f"agent exited with code {code}"
        from . import models
        from .errors import MODEL_UNAVAILABLE
        classification = adapter.classify_error(failure, code)
        rejected_model = classification.kind == MODEL_UNAVAILABLE
        if rejected_model:
            models.reject(conn, process["provider"], launch["env"].get("AGENTKIT_MODEL", ""), failure)
            if process["purpose"] == "coordinator":
                from .jobs import initial_model_rejected
                initial_model_rejected(root, conn, process["job_id"])
        if process["purpose"] == "worker":
            if rejected_model:
                task = db.get_task(conn, process["task_id"])
                if task and task["generation"] == process["generation"]:
                    from .policy import record_trigger
                    record_trigger(conn, task["id"], MODEL_UNAVAILABLE, process["provider"],
                                   launch["env"].get("AGENTKIT_MODEL", ""))
                    db.update_task(conn, task["id"], session_token=None)
                    if process.get("worker_run_id"):
                        db.close_worker_run(conn, process["worker_run_id"], exit_code=code)
                    db.set_status(conn, task["id"], "READY", cause="model rejected; select another eligible model")
            elif degraded:
                _essential(lambda: db.log_event(
                    conn, process["task_id"], "monitor_error", cause=degraded,
                    effect="worker was not stopped; task held for manager review",
                    detail={"exit_code": code}))
                _essential(lambda: _hold_for_host_fault(conn, root, process, code, degraded))
            else:
                _essential(lambda: finish_worker(conn, root, process, code, failure))
        elif code or failure:
            if process["purpose"] == "coordinator" and "session" in failure.lower() and any(
                text in failure.lower() for text in ("not found", "invalid", "missing")
            ):
                conn.execute("UPDATE jobs SET coordinator_session=NULL WHERE id=?", (process["job_id"],))
                db.log_event(conn, None, "coordinator_session_recovery", cause="unavailable session; same provider will reload durable job memory", detail={"job": process["job_id"]})
            classification = adapter.classify_error(failure, code)
            if classification.is_provider_problem:
                providers.observe(conn, process["provider"], {"available": False,
                    "reason": classification.reason, "auth_error": classification.kind == "AUTH_ERROR",
                    "retry_at": classification.retry_at.isoformat() if classification.retry_at else None})
        if degraded:
            failure = f"{failure}\n{degraded}" if failure else degraded
    except Exception as exc:
        # The monitor failed outside a guarded section. A worker it can no longer
        # supervise is stopped, but the fault stays AgentKit's: the real exit code
        # is kept, no attempt is consumed, and the task is held for the manager.
        failure = f"monitor error: {exc}"
        stopped = False
        if child and child.poll() is None:
            stopped = True
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if child and child.returncode is not None:
            code = child.returncode
        with suppress(sqlite3.Error):
            db.log_event(conn, process["task_id"], "monitor_error", cause=failure,
                         effect="monitor stopped the worker" if stopped else "worker had already exited",
                         detail={"exit_code": code})
        if process["purpose"] == "worker":
            try:
                _essential(lambda: _hold_for_host_fault(conn, root, process, code, failure))
            except Exception as recovery_error:
                with suppress(sqlite3.Error):
                    db.log_event(conn, process["task_id"], "monitor_recovery_error", cause=str(recovery_error))
    finally:
        from .usage_receipts import capture
        capture(root, identifier)
        _essential(lambda: processes.update(
            conn, identifier, status="FAILED" if code or failure else "FINISHED",
            exit_code=code, result=redact_text("\n".join(answers))[-30000:],
            error=redact_text(failure), ended_at=db.utcnow(), heartbeat_at=db.utcnow(),
            child_launch_state=("WSL_UNCONFIRMED" if (processes.get(conn, identifier) or {}).get("child_launch_state") == "WSL_UNCONFIRMED" else "EXITED" if child else "NOT_STARTED")))
        from .recovery_runtime import finished
        _essential(lambda: finished(conn, root, processes.get(conn, identifier), code, failure))
        if child:
            for stream, thread in locals().get("readers", []):
                thread.join(timeout=0.2)
                if stream and not thread.is_alive():
                    stream.close()  # Never wait on the lock of a blocked pipe reader.
        conn.close()
    return code


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]).resolve(), int(sys.argv[2])))
