"""Bridge the shared recovery ledger to existing guarded CLI launches and monitors."""
from __future__ import annotations

import json
from pathlib import Path

from . import adapters, db, processes, providers, repo, wake_adapters
from . import recovery_store as store
from .config import load_project


def process_session(conn, process):
    runtime = adapters.get(process["provider"])
    return store.register(conn, identifier=f"cli:{process['id']}", provider=process["provider"],
                          account=runtime.account_id() if runtime else "default", host="cli",
                          role="manager" if process["purpose"] == "coordinator" else
                          "reviewer" if process["purpose"] == "review" else "worker",
                          reference=str(process["id"]), job_id=process.get("job_id"),
                          task_id=process.get("task_id"), process_id=process["id"],
                          policy="unmetered" if providers.unmetered(process["provider"]) else "subscription",
                          identity={key: process.get(key) for key in
                                    ("pid", "pid_identity", "child_pid", "child_pid_identity", "session_token")})


def proof(conn, root, task_id=None, job_id=None):
    from .manager_state import launch_signature, state
    task = db.get_task(conn, task_id) if task_id else None
    job_id = job_id or (task or {}).get("job_id")
    runtime = conn.execute("SELECT revision,planned_revision,status FROM jobs WHERE id=?", (job_id,)).fetchone()
    work = (task or {}).get("worktree")
    value = {"revision": dict(runtime) if runtime else None, "worktree": work,
             "generation": (task or {}).get("generation"), "branch": (task or {}).get("branch")}
    if task:
        value["signature"] = launch_signature(conn, task)
    else:
        value["epoch"] = state(conn, job_id)["epoch"]
    if work:
        from .worktree_digest import fingerprint
        try:
            value.update(head=repo.head_commit(work), dirty=fingerprint(work))
        except (OSError, ValueError):
            value.update(unavailable=True)
    return value


def pending(conn, *, task_id=None, job_id=None):
    query = "SELECT i.id FROM recovery_intents i JOIN recovery_sessions s ON s.id=i.session_id "
    query += "WHERE i.state NOT IN ('RECOVERED','CANCELLED','NEEDS_USER_ACTION') AND "
    query += "s.task_id=? " if task_id else "s.role='manager' AND s.job_id=? "
    row = conn.execute(query + "ORDER BY i.created_at DESC LIMIT 1", (task_id or job_id,)).fetchone()
    return store.get(conn, row[0]) if row else None


def park_worker(conn, task, meta):
    root = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent.parent
    row = conn.execute("SELECT * FROM processes WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if row:
        session_id = process_session(conn, dict(row))
    else:
        session_id = store.register(conn, identifier=f"queued:{task['id']}:{task['generation']}",
            provider=meta["provider"], account=meta.get("account", "default"), host="cli", role="worker",
            reference=f"task:{task['id']}:{task['generation']}", task_id=task["id"], job_id=task.get("job_id"),
            policy="unmetered" if providers.unmetered(meta["provider"]) else "subscription")
    saved = proof(conn, root, task_id=task["id"])
    authorization = json.dumps({"generation": task["generation"], "revision": saved["revision"],
                                "paused_at": meta.get("paused_at")}, sort_keys=True)
    intent = store.arm(conn, session_id, authorization, saved,
                       "health" if providers.unmetered(meta["provider"]) else "quota")
    if saved.get("unavailable") and intent["state"] not in store.TERMINAL:
        store.move(conn, intent["id"], "NEEDS_USER_ACTION", "Preserved checkout cannot be inspected; repair on host")
    return store.get(conn, intent["id"])


def account_evidence(conn, provider, account):
    state = providers.get_state(conn, provider, account)
    windows = [dict(r) for r in conn.execute(
        "SELECT window,used_percent,resets_at FROM quota_windows WHERE account_key=?",
        (providers.account_key(provider, account),))]
    return {"observed_at": state.detected_at, "available": state.status == providers.AVAILABLE,
            "healthy": state.status == providers.AVAILABLE, "complete": state.status == providers.AVAILABLE,
            "windows": windows, "provider": provider, "account": account}


def authorize(conn, root, intent, *, provider=None, allow_generation_step=False):
    session = store.session(conn, intent["session_id"])
    task_id, job_id = session["task_id"], session["job_id"]
    current = proof(conn, root, task_id, job_id)
    if intent["proof"].get("worktree") is None and current.get("worktree") and allow_generation_step:
        # No preserved source exists for a task parked before its first launch.
        # The guarded scheduler just provisioned its declared fresh checkout.
        saved = {**intent["proof"], **{key: current[key] for key in ("worktree", "head", "dirty", "branch")}}
        conn.execute("UPDATE recovery_intents SET proof=? WHERE id=?", (store.encode(saved), intent["id"]))
        intent["proof"] = saved
    if allow_generation_step and current["generation"] == (intent["proof"]["generation"] or 0) + 1:
        current["generation"] = intent["proof"]["generation"]
    project = load_project(root)
    task = db.get_task(conn, task_id) if task_id else None
    authorized = project.raw.get("execution_paused") is not True
    authorized = authorized and (not task or task["status"] not in ("CANCELLED", "DONE", "NEEDS_REPLAN"))
    if session["job_id"]:
        authorized = authorized and bool(current["revision"] and current["revision"]["status"] == "ACTIVE")
    owners = processes.owning(conn)
    stopped = not any((task_id and p["task_id"] == task_id) or
                      (not task_id and p["job_id"] == job_id and p["purpose"] == "coordinator") for p in owners)
    target = provider or session["provider"]
    runtime = adapters.get(target, project)
    account = runtime.account_id() if runtime else "default"
    evidence = account_evidence(conn, target, account)
    if providers.unmetered(target):
        # A subscription->local handoff still uses the target's explicit health policy.
        evidence.update(available=evidence["healthy"], complete=True, windows=[])
    return wake_adapters.ready(conn, intent["id"], evidence, authorized=authorized,
                               old_owner_stopped=stopped, proof=current), current


def worker_ready(conn, task, meta):
    root = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent.parent
    intent = pending(conn, task_id=task["id"]) or park_worker(conn, task, meta)
    return authorize(conn, root, intent)[0]


def before_launch(conn, root, *, purpose, provider, task_id=None, job_id=None):
    intent = pending(conn, task_id=task_id, job_id=job_id)
    if not intent:
        return None
    valid, current = authorize(conn, root, intent, provider=provider, allow_generation_step=True)
    if not valid:
        raise ValueError("Recovery intent is not ready; inspect authorization, allowance and ownership")
    attempt = store.claim(conn, intent["id"], "guarded-cli-launch", proof=current,
                          authorization=intent["authorization"])
    if not attempt:
        raise ValueError("Recovery delivery already claimed; reconcile before launching")
    return intent["id"], attempt


def launched(conn, identifier, claim=None):
    process_session(conn, processes.get(conn, identifier))
    if claim:
        intent, attempt = claim
        conn.execute("UPDATE recovery_intents SET delivery_process=? WHERE id=?", (identifier, intent))
        store.move(conn, intent, "DELIVERY_ACCEPTED", "Guarded CLI monitor launch accepted; turn start not yet observed",
                   attempt_id=attempt, evidence={"delivery_process": identifier})


def turn_started(conn, identifier):
    row = conn.execute("SELECT id,attempt_id FROM recovery_intents WHERE delivery_process=?", (identifier,)).fetchone()
    if row:
        current = store.get(conn, row["id"])
        if current["state"] not in store.TERMINAL:
            store.move(conn, row["id"], "TURN_STARTED", "Provider session-start event observed", attempt_id=row["attempt_id"])
    process_session(conn, processes.get(conn, identifier))


def finished(conn, root, process, code, failure):
    process_session(conn, process)
    row = conn.execute("SELECT id,attempt_id FROM recovery_intents WHERE delivery_process=?", (process["id"],)).fetchone()
    if row:
        current = store.get(conn, row["id"])
        if current["state"] not in store.TERMINAL:
            success = not code and not failure and bool(current["started_at"])
            store.move(conn, row["id"], "RECOVERED" if success else "NEEDS_USER_ACTION",
                       "Recovered turn completed; task gates and review remain separate" if success
                       else "Delivered turn failed or start was not observed; preserve checkpoint",
                       attempt_id=row["attempt_id"])
    if process["purpose"] == "coordinator" and (code or failure):
        runtime = adapters.get(process["provider"])
        classification = runtime.classify_error(failure, code) if runtime else None
        if classification and classification.is_provider_problem:
            saved = proof(conn, root, job_id=process["job_id"])
            store.arm(conn, f"cli:{process['id']}", str(saved["epoch"]), saved,
                      "health" if providers.unmetered(process["provider"]) else classification.kind)


def reconcile(conn, root):
    store.reconcile_expired(conn)
    paused = load_project(root).raw.get("execution_paused") is True
    for item in store.snapshot(conn, limit=500):
        if item["state"] in store.TERMINAL:
            continue
        task = db.get_task(conn, item["task_id"]) if item["task_id"] else None
        if paused or (task and task["status"] in ("CANCELLED", "DONE", "NEEDS_REPLAN")):
            store.cancel(conn, item["id"], "Project paused or assignment ended/superseded")
            continue
        if item["host"] == "cli" and item["state"] in ("WAITING_AVAILABILITY", "READY_TO_WAKE"):
            intent = store.get(conn, item["id"])
            # Worker recovery is checked only when scheduling it, avoiding repeated git audits.
            if not item["task_id"]:
                authorize(conn, root, intent)


def supersede_worker_intents(conn, task_id, reason):
    """Explicit planner retry retires old claims, preserving their proofs and journal."""
    if any(p['task_id'] == task_id for p in processes.owning(conn)):
        raise ValueError('Previous worker ownership must be stopped')
    rows = conn.execute('SELECT i.id FROM recovery_intents i JOIN recovery_sessions s '
                        'ON s.id=i.session_id WHERE s.task_id=? '
                        "AND i.state NOT IN ('RECOVERED','CANCELLED','NEEDS_USER_ACTION')",
                        (task_id,)).fetchall()
    for row in rows:
        store.cancel(conn, row[0], 'Planner authorized fresh guarded retry: ' + reason)
    return len(rows)
