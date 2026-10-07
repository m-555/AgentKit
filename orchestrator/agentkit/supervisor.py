"""Durable coordination around a pinned coordinator and replaceable workers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import (
    adapters,
    db,
    instructions,
    integrator,
    jobs,
    processes,
    providers,
    quota,
    repo,
    scheduler,
)
from .capabilities import load_cache
from .config import load_project
from .locking import exclusive
from .reconcile import pid_alive


def recover_monitors(conn, root):
    from .runner import finish_worker
    now = datetime.now(UTC)
    for process in processes.owning(conn):
        started = db.parse_ts(process["started_at"]) or now
        if process["status"] == "STARTING" and (now - started).total_seconds() < 60:
            continue
        if pid_alive(process.get("pid")):
            continue
        if pid_alive(process.get("child_pid")):
            # A surviving child still owns its work. Never launch a competing session.
            if process["task_id"]:
                db.heartbeat(conn, process["task_id"], process["generation"])
            continue
        # The ownership snapshot may predate the monitor final exit write.
        process = processes.get(conn, process["id"]) or process
        if (process["status"] not in ("STARTING", "RUNNING")
                and process.get("ended_at")
                and process.get("child_launch_state") != "WSL_UNCONFIRMED"):
            continue
        from . import transport_ownership
        if transport_ownership.is_wsl(process):
            if not transport_ownership.recover_exit(root, process["id"]):
                continue  # Preserve ownership until Linux termination is proven.
            processes.update(conn, process["id"], child_launch_state="EXITED")
            process = processes.get(conn, process["id"]) or process
        if not process.get("pid") or processes.ownership_uncertain(process):
            db.log_event(conn, process.get("task_id"), "process_ownership_uncertain",
                         cause="missing monitor or child identity; explicit ownership audit required")
            continue
        code = process.get("exit_code")
        code = 1 if code is None else code
        error = process.get("error") or ("monitor and child exited unexpectedly" if code else "")
        if process["purpose"] == "worker":
            finish_worker(conn, root, process, code, error)
        processes.update(conn, process["id"], status="FAILED" if code or error else "FINISHED",
                         ended_at=db.utcnow(), exit_code=code, error=error)


def refresh_accounts(conn, project, names):
    """Coalesce account checks; never ping once for each worker sharing an account."""
    now = datetime.now(UTC)
    interval = int(project.raw.get("availability_poll_seconds", 300))
    for name in sorted(set(names)):
        adapter = adapters.get(name, project)
        if adapter is None:
            continue
        state = providers.get_state(conn, name, adapter.account_id())
        observed = db.parse_ts(state.detected_at)
        if state.status == providers.AUTH_REQUIRED:
            continue
        if state.status == providers.AVAILABLE and getattr(adapter, "availability_requires_inference", False):
            continue  # Healthy Claude worker events provide evidence; do not burn tokens on pings.
        requires_inference = getattr(adapter, "availability_requires_inference", False)
        if state.status != providers.AVAILABLE and not state.due(now) and requires_inference:
            continue  # Do not spend inference on early cooldown checks.
        if observed and (now - observed).total_seconds() < interval:
            continue  # Metadata checks can detect manual resets before the old deadline.
        providers.observe(conn, name, adapter.check_availability(), account=adapter.account_id())


def handoff_waiting(conn, root):
    from . import models
    capabilities = load_cache(root)
    unavailable = scheduler.unavailable_adapters(conn)
    active_tasks = {p["task_id"] for p in processes.owning(conn)}
    for task in quota.paused_tasks(conn):
        if task["id"] in active_tasks:
            continue
        adapter, reason = models.choose_worker(conn, load_project(root), task, capabilities, unavailable=unavailable)
        if adapter:
            # Actual scope/ancestry/liveness audit happens before launch, including
            # for the same provider. Keep original adapter until that audit passes.
            # Selection only queues a permitted continuation. The guarded launch
            # claims its recovery intent after fresh account and ownership checks.
            db.update_task(conn, task["id"], blocked_meta=None, blocker=None)
            db.set_status(conn, task["id"], "READY", cause="provider available for audited continuation: " + reason)


def _control_launch(conn, project, job, purpose, task=None):
    from . import manager, manager_state, models
    if purpose == "coordinator" and (manager.blocks_spawn(conn, job["id"]) or any(
            p["purpose"] == "coordinator" and p["job_id"] == job["id"] for p in processes.owning(conn))):
        return
    selected = None
    try:
        candidates = models.control_candidates(project, job, purpose)
    except ValueError as exc:
        # A bad explicit policy must be visible, never replaced by a default model.
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (str(exc), job["id"]))
        db.log_event(conn, task.get("id") if task else None, "control_policy_error", cause=str(exc),
                     detail={"job": job["id"], "purpose": purpose})
        return
    for candidate in candidates:
        runtime_adapter = adapters.get(candidate.provider, project)
        if models.usable(conn, project, candidate, pinned=bool(purpose == "coordinator" and job.get("coordinator_model"))) and runtime_adapter and runtime_adapter.detect():
            selected = candidate
            break
    if selected is None:
        return
    name = selected.provider
    adapter = adapters.get(name, project)
    if not adapter or not adapter.detect():
        return
    issue = adapter.runtime_problem(selected.model)
    if issue:
        # An outdated CLI is a setup blocker; trying the next model would hide it.
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (issue, job["id"]))
        db.log_event(conn, task.get("id") if task else None, "runtime_incompatible", cause=issue,
                     detail={"job": job["id"], "purpose": purpose, "model": selected.to_dict()})
        return
    if purpose == "coordinator":
        job = jobs.pin_coordinator(project.root, job["id"], selected)
    # job_id lets the launch re-validate against the durable coordinator pin.
    task = {**(task or {}), "job_id": job["id"], "_model_selection": selected.to_dict()}
    role = "coordinator" if purpose == "coordinator" else "reviewer"
    work = Path(task.get("worktree") or project.root)
    if purpose == "coordinator":
        body = (f"Call job_brief for {job['id']}. Interpret all user requests, inspect the repository, "
                "and plan or repair this job. Use task_define and job_decision. Finish with "
                "job_plan_ready referencing the current revision. If every required task is DONE, "
                "review the combined result against all job acceptance criteria and use job_accept; "
                "otherwise define follow-up tasks. Never replace the coordinator provider.")
        runtime = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        token = runtime["coordinator_session"]
        combined = integrator.integration_worktree(project)
        body += (f"\nThe combined result is in {combined} at {repo.head_commit(combined)}. "
                 "Read that checkout for final acceptance; the operator checkout may contain a different revision.")
        if manager_state.pending(conn, job["id"]):
            import json

            from .workflow import enabled
            recovery_context = "" if enabled(project) else json.dumps(manager.packet(conn, job["id"]), default=str)
            body += ("\nRecovery barrier is active. First call manager_audit for this job, inspect every finding and "
                     "preserve user amendments and failed tasks. Repair findings explicitly, then call manager_ack "
                     "with the returned current epoch and digest plus concrete evidence. No integration or newly "
                     "dependent launches can occur before this acknowledgement.\n" + recovery_context)
        head = None
    else:
        head = repo.head_commit(work)
        body = (f"Independently review task {task['id']} at commit {head}. Call brief, task_diff and audit_diff, "
                "read its complete diff against the recorded base, check job intent and acceptance criteria. "
                f"Call review_submit(task_id={task['id']}, head_sha='{head}', verdict='PASS' or 'CHANGES' "
                "or 'REJECT', evidence=concrete findings). Do not fix source or approve a different commit.")
    body += ("\nIf your runtime cannot run shell commands or read files, use the read-only AgentKit tools "
             "source_list, source_read, source_search and source_diff; they serve exact commits. Never "
             "claim to have read code you could not open.")
    if purpose != "coordinator":
        token = None
    prompt = instructions.prompt({**task, "job_id": job["id"]}, project, body, role=role)
    launch = adapter.build_launch(task, work, role, project, prompt=prompt, resume_token=token)
    launch.env.pop("AGENTKIT_TASK", None)
    launch.env.pop("AGENTKIT_GENERATION", None)
    if purpose == "review":
        launch.env["AGENTKIT_TASK"] = str(task["id"])
        launch.env["AGENTKIT_GENERATION"] = str(task["generation"])
    launch.env["AGENTKIT_JOB"] = job["id"]
    identifier = processes.start(conn, project.root, launch, purpose=purpose, provider=name,
        task_id=task.get("id"), job_id=job["id"], expected_head=head)
    db.log_event(conn, task.get("id"), "control_session_started",
                 detail={"process": identifier, "purpose": purpose, "provider": name, "job": job["id"], "model": selected.to_dict()})
    return identifier


def tick(root, *, dry_run=False) -> list[str]:
    """One finite pass. Timers and all authority survive process restarts."""
    if dry_run:
        return ["dry run: supervision, checks and control launches skipped"]
    with exclusive(root, "supervisor"):
        return _tick(Path(root))


def _tick(root: Path):
    project = load_project(root)
    from . import planner_events, review_policy, user_acceptance
    review_mode = review_policy.mode(project)
    if project.raw.get("execution_paused") is True:
        return ["execution paused: model checks, launches, retries and integration skipped"]
    from . import native_recovery_service, native_session_registration, recovery_runtime
    if any(item.get('enabled') for item in native_session_registration.snapshot(root)):
        native_recovery_service.start(root)  # Restart only host cadence, never an AI owner.
    conn = db.connect(root)
    notes = []
    try:
        recovery_runtime.reconcile(conn, root)
        recover_monitors(conn, root)
        from .usage_receipts import capture_stopped
        capture_stopped(root, conn)
        try:
            with exclusive(root, "integration", timeout=0):
                for task in db.list_tasks(conn, ("INTEGRATING",)):
                    if task.get("blocker") == "manager recovery pending":
                        continue
                    db.update_task(conn, task["id"], blocker="interrupted integration",
                                   next_action="Automatically retry exact approved commit and combined checks; preserve conflicts.")
        except TimeoutError:
            pass  # A live integrator still owns these transitions.
        definitions = []
        for path in sorted((root / ".ai" / "jobs").glob("*.json")):
            job = jobs.load(root, path.stem)
            jobs.sync(root, job)
            definitions.append(job)
        names = list(load_cache(root))
        for job in definitions:
            names.extend([job["coordinator"], *job["reviewers"]])
        from . import manager, manager_state
        manager_state.capture_current(conn)
        refresh_accounts(conn, project, names)
        from . import native_session_recovery
        notes.extend(native_session_recovery.tick(conn, root))
        handoff_waiting(conn, root)
        from .integration_remediation import release_ready
        notes.extend(release_ready(conn, project))
        for job in definitions:
            runtime = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
            if runtime["status"] in ("DONE", "BLOCKED"):
                continue
            if job.get("workflow_review", review_mode) != review_mode:
                conn.execute("UPDATE jobs SET status='BLOCKED',last_error='Saved review policy changed; restore configuration before replanning' WHERE id=?", (job["id"],))
                continue
            active = processes.owning(conn)
            coordinator_active = manager.blocks_spawn(conn, job["id"]) or any(p["job_id"] == job["id"] and p["purpose"] == "coordinator" for p in active)
            tasks = [t for t in db.list_tasks(conn) if t.get("job_id") == job["id"]]
            active_ids = {p["task_id"] for p in active}
            for task in tasks:
                if task["id"] in active_ids:
                    continue
                if task["status"] == "REVIEW":
                    from .external_review import enabled
                    if review_mode == "human":
                        try:
                            review_policy.stage(conn, project, task)
                        except (ValueError, PermissionError) as exc:
                            db.update_task(conn, task["id"], blocker=str(exc), next_action="Planner must resolve staging: " + str(exc))
                    elif not enabled(project):
                        _control_launch(conn, project, job, "review", task)
                elif task["status"] == "FAILED" and task["attempts"] < 3 and not manager_state.pending(conn, job["id"]) and project.raw.get("workflow", {}).get("mode") != "separate-tasks":
                    db.update_task(conn, task["id"], attempts=task["attempts"] + 1)
                    db.set_status(conn, task["id"], "READY", cause="retry with recorded failure evidence")
            needs_plan = manager_state.pending(conn, job["id"]) or runtime["planned_revision"] != job["revision"] or not tasks
            needs_plan |= any(t["status"] in ("NEEDS_REPLAN", "STALE") or
                              (t["status"] == "FAILED" and (t["attempts"] >= 3 or project.raw.get("workflow", {}).get("mode") == "separate-tasks")) for t in tasks)
            needs_plan |= any((t["status"] == "BLOCKED" and not quota.is_quota_paused(t)) or
                              (t["status"] in ("READY", "REVIEW") and t.get("blocker")) for t in tasks)
            needs_plan |= review_mode != "human" and bool(tasks) and all(t["status"] in ("DONE", "CANCELLED") for t in tasks)
            needs_plan |= bool(runtime.get("last_error", "") and str(runtime["last_error"]).startswith("preview:"))
            needs_plan |= any(a.get("task_id") in {t["id"] for t in tasks} for a in db.list_amendments(conn))
            check = db.parse_ts(runtime.get("next_check"))
            if needs_plan and not coordinator_active and (not check or check <= datetime.now(UTC)) and planner_events.should_dispatch(conn, project, job, tasks):
                identifier = _control_launch(conn, project, job, "coordinator")
                planner_events.record(conn, project, job, tasks, identifier)
                conn.execute("UPDATE jobs SET next_check=? WHERE id=?",
                             ((datetime.now(UTC) + timedelta(seconds=60)).isoformat(), job["id"]))
        for task in integrator.queue(conn):
            # User corrections suspend integration until the coordinator replans.
            if manager_state.pending(conn, task.get("job_id")):
                continue
            if task.get("job_id"):
                state = conn.execute("SELECT * FROM jobs WHERE id=?", (task["job_id"],)).fetchone()
                if state["status"] != "ACTIVE" or state["revision"] != state["planned_revision"]:
                    continue
            if any(p["task_id"] == task["id"] for p in processes.owning(conn)):
                continue
            outcome = integrator.merge_one(conn, project, task)
            notes.append(outcome.summary())
        if review_mode == "human":
            for job in definitions:
                user_acceptance.prepare(conn, project, job["id"])
        return notes
    finally:
        conn.close()


def accept_job(root, job_id: str, revision: int, evidence: str) -> str:
    project = load_project(root)
    from . import review_policy, verification
    if review_policy.mode(project) == "human":
        raise PermissionError("human-review jobs require operator acceptance of the exact preview")
    job = jobs.load(root, job_id)
    if job.get("workflow_review") == "human":
        raise PermissionError("saved human-review policy requires operator acceptance")
    if revision != job["revision"] or not evidence.strip():
        raise ValueError("acceptance must address the current job revision with evidence")
    with exclusive(root, "integration"):
        conn = db.connect(root)
        try:
            from . import manager_state
            manager_state.require_clear(conn, job_id)
            tasks = [t for t in db.list_tasks(conn) if t.get("job_id") == job_id]
            required = [t for t in tasks if t["status"] != "CANCELLED"]
            if not required or any(t["status"] != "DONE" for t in required):
                raise ValueError("every required task must be DONE; cancelled tasks require replanning")
            work = integrator.integration_worktree(project)
            if not repo.is_clean(work):
                raise ValueError("integration worktree is dirty")
            head = repo.head_commit(work)
            from .review_policy import STAGING_REVIEWER
            for task in required:
                approval = conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
                if not approval or approval["verdict"] != "PASS" or approval["reviewer"] == STAGING_REVIEWER or not repo.is_ancestor(work, approval["head_sha"], head):
                    raise ValueError("AI acceptance requires independent approved commits in the combined result")
            result = verification.run(conn, project, work, "full")
            if not result.passed or repo.head_commit(work) != head or not repo.is_clean(work):
                raise ValueError("combined result failed verification: " + result.summary())
            with db.immediate_transaction(conn):
                manager_state.require_clear(conn, job_id)
                state = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not state or state["status"] != "ACTIVE" or state["planned_revision"] != revision:
                    raise ValueError("job acceptance requires the current active plan")
                if jobs.load(root, job_id)["revision"] != revision:
                    raise ValueError("user intent changed during acceptance gate")
                conn.execute("UPDATE jobs SET status='DONE',completed_sha=?,last_error=NULL,updated_at=? WHERE id=?", (head, db.utcnow(), job_id))
                db.log_event(conn, None, "job_accepted", cause=evidence, detail={"job": job_id, "head": head, "revision": revision})
            return head
        finally:
            conn.close()
