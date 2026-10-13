"""Durable coordination around a pinned coordinator and replaceable workers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import (
    adapters,
    db,
    instructions,
    integrator,
    job_acceptance,
    jobs,
    planner_wake,
    problems,
    processes,
    providers,
    quota,
    repo,
    review_retry,
    scheduler,
    workflow,
)
from .capabilities import load_cache
from .config import load_project
from .control_prompts import REVIEW_REFUSED
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
        if state.status in (providers.AUTH_REQUIRED, providers.HELD):
            continue  # a human must act; no check can lift either
        if state.status == providers.AVAILABLE and getattr(adapter, "availability_requires_inference", False):
            from .recovery_runtime import waiting_for
            # Healthy Claude worker events provide evidence; do not burn tokens on pings,
            # unless a parked task needs fresh evidence to resume (F25). The interval
            # check below still limits that to one ping per poll interval.
            if not waiting_for(conn, name, adapter.account_id()):
                continue
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


def coordinator_body(conn, project, job, *, resumed: bool) -> str:
    """A fresh manager plans from the job brief; a resumed one reads only what changed."""
    if resumed:
        from .manager_digest import wake_prompt
        return wake_prompt(conn, project, job)
    from .manager_digest import digest
    finish = ("You are the assigner: turn the requests into tasks, then finish. Builders test their own work; "
              "review and acceptance run without you, and anything that stops waits for the user."
              if workflow.enabled(project) else
              "If every required task is DONE, review the combined result against all job acceptance criteria "
              "and use job_accept; otherwise define follow-up tasks.")
    body = (f"Call job_brief for {job['id']}: it is compact by default and complete for planning (every "
            "user request, acceptance, model pins and unfinished tasks). Ask include_history=True only to "
            "investigate a specific unresolved finding. Interpret all user requests, inspect the repository, "
            "and plan or repair this job. Use task_define and job_decision. Finish with "
            f"job_plan_ready referencing the current revision. {finish} Never replace the coordinator provider.")
    note = workflow.planning_note(project)
    return body + ("\n" + note if note else "") + "\nCurrent state:\n" + digest(conn, project, job)


#: A resumed manager re-sends its whole history every turn; past this many input tokens in its
#: last session, a fresh start with the compact brief and the digest costs less.
RESUME_INPUT_LIMIT = 600_000


def manager_session(conn, root, job_id: str) -> str | None:
    """The manager session to resume, or None when its last session grew too large."""
    row = conn.execute("SELECT coordinator_session FROM jobs WHERE id=?", (job_id,)).fetchone()
    token = row["coordinator_session"] if row else None
    if not token:
        return None
    last = conn.execute("SELECT id,provider,status FROM processes WHERE purpose='coordinator' AND job_id=? "
                        "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    from .job_usage import process as process_usage
    sent = process_usage(root, last)[0] if last else None
    if sent is not None and sent > RESUME_INPUT_LIMIT:
        conn.execute("UPDATE jobs SET coordinator_session=NULL WHERE id=?", (job_id,))
        db.log_event(conn, None, "manager_fresh_start", cause=f"last manager session read {sent:,} input tokens",
                     effect="next wake starts fresh with the compact brief and digest",
                     detail={"job": job_id, "process": last["id"]})
        return None
    return token


def review_body(conn, project, task, head: str, *, limit: int | None = None) -> str:
    """The reviewer's instruction plus the exact commit's packet (diff, scope, gate receipt)."""
    from .review_packet import build
    return (f"Independently review task {task['id']} at commit {head}, using the packet below. Check job "
            "intent and acceptance criteria; read more (brief, source_read, task_diff) only where needed. "
            f"Call review_submit(task_id={task['id']}, head_sha='{head}', verdict='PASS' or 'CHANGES' "
            "or 'REJECT', evidence=concrete findings). Do not fix source or approve a different commit. "
            "BLOCKED means you could not complete the review because a tool, permission, gate runner or "
            "setup failed; the task waits for the user and the builder is never sent back. CHANGES and "
            "REJECT are only for defects you found in the code.\n" + build(conn, project, task, head, limit=limit))


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
    work = integrator.integration_worktree(project) if purpose == "final_review" else Path(task.get("worktree") or project.root)
    if purpose == "coordinator":
        token = manager_session(conn, project.root, job["id"])
        body = coordinator_body(conn, project, job, resumed=bool(token))
        from .job_input import names
        shots = names(project.root, job["id"])
        if shots:
            body += (f"\nThe user attached screenshots ({', '.join(shots)}); open the ones a request names with "
                     f"job_attachment(job_id='{job['id']}', name=...) before planning, and name them in each task "
                     "that needs them.")
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
        from .external_review import standin_note
        from .manager_chat import pending, prompt_section
        body += standin_note(conn, project, job["id"])
        chat = [m["id"] for m in pending(project.root, job["id"])]
        body += prompt_section(project.root, job["id"])
        head = None
    elif purpose == "final_review":
        head = combined_head = repo.head_commit(work)
        intent = jobs.load(project.root, job["id"])
        members = [t for t in db.list_tasks(conn) if t.get("job_id") == job["id"]]

        def build(size):
            return job_acceptance.body(conn, project, intent, members, combined_head, limit=size)
    else:
        head = task_head = repo.head_commit(work)

        def build(size):
            return review_body(conn, project, task, task_head, limit=size)
    if purpose != "coordinator":
        token = None
    from .control_prompts import TAIL, fitted, refused
    try:
        prompt = (instructions.prompt({**task, "job_id": job["id"]}, project, body + TAIL, role=role)
                  if purpose == "coordinator" else fitted(project, task, job, role, build))
    except ValueError as exc:  # never escape the supervisor's pass: the launch waits for the user
        refused(conn, job, purpose, task, str(exc))
        return None
    launch = adapter.build_launch(task, work, role, project, prompt=prompt, resume_token=token)
    launch.env.pop("AGENTKIT_TASK", None)
    launch.env.pop("AGENTKIT_GENERATION", None)
    if purpose == "review":
        launch.env["AGENTKIT_TASK"] = str(task["id"])
        launch.env["AGENTKIT_GENERATION"] = str(task["generation"])
    launch.env["AGENTKIT_JOB"] = job["id"]
    identifier = processes.start(conn, project.root, launch, purpose=purpose, provider=name,
        task_id=task.get("id"), job_id=job["id"], expected_head=head)
    if purpose == "coordinator" and chat:
        from .manager_chat import mark_delivered
        mark_delivered(project.root, job["id"], chat, to=f"coordinator process {identifier}")
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
            from .job_budget import enforce
            if enforce(conn, project, job["id"], runtime):
                continue  # held for its token budget; running sessions finish
            runtime = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
            if runtime["status"] in ("DONE", "BLOCKED"):
                continue
            # Switching between AI, once and off is safe mid-job; switching to or from human review is not.
            if (job.get("workflow_review", review_mode) == "human") != (review_mode == "human"):
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
                    elif review_mode in ("once", "off"):
                        if not task.get("blocker"):  # a failed approval waits for the user, not the next pass
                            try:
                                review_policy.pass_gate(conn, project, task)
                            except (ValueError, PermissionError) as exc:
                                db.update_task(conn, task["id"], blocker=str(exc),
                                               next_action="Checks must pass before this task merges: " + str(exc))
                    elif not enabled(project) and not str(task.get("blocker") or "").startswith(REVIEW_REFUSED):
                        _control_launch(conn, project, job, "review", task)
                elif task["status"] == "FAILED" and not manager_state.pending(conn, job["id"]) and review_retry.due(conn, task):
                    review_retry.requeue(conn, task)  # routine findings never wake the manager
                elif (task["status"] == "FAILED" and task["attempts"] < 3 and not manager_state.pending(conn, job["id"])
                      and problems.retries_itself(conn, project, task)):
                    db.update_task(conn, task["id"], attempts=task["attempts"] + 1)
                    db.set_status(conn, task["id"], "READY", cause="retry with recorded failure evidence")
            tasks = [t for t in db.list_tasks(conn) if t.get("job_id") == job["id"]]  # statuses after retries
            needs_plan = planner_wake.needs_plan(conn, project, job, runtime, tasks, review_mode, active_ids)
            accepted = job_acceptance.step(conn, project, job, runtime, tasks,
                                           lambda job=job: _control_launch(conn, project, job, "final_review"))
            if accepted:
                notes.append(accepted)
                continue
            check = db.parse_ts(runtime.get("next_check"))
            from .manager_chat import pending as chat_pending
            waiting_chat = bool(chat_pending(project.root, job["id"]))  # the user wrote in the view chat
            if (needs_plan or waiting_chat) and not coordinator_active and (not check or check <= datetime.now(UTC)) and (
                    waiting_chat or planner_events.should_dispatch(conn, project, job, tasks)):
                identifier = _control_launch(conn, project, job, "coordinator")
                planner_events.record(conn, project, job, tasks, identifier)
                conn.execute("UPDATE jobs SET next_check=? WHERE id=?",
                             ((datetime.now(UTC) + timedelta(seconds=60)).isoformat(), job["id"]))
        problems.sync(conn, project)  # a stop waits for the user; the rest of each job continues
        try:
            from .phone import tick as phone_tick
            notes.extend(phone_tick(conn, project))  # new problems to the user's phone; their replies back
        except Exception as exc:  # a mail bug must never stop supervision; it is logged and retried
            db.log_event(conn, None, "phone_error", cause=str(exc)[:300], effect="mail skipped this pass")
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
        from .auto_probe import tick as reprobe
        notes.extend(reprobe(conn, project))  # A changed CLI or guard is re-proven, not waited on (F31).
        return notes
    finally:
        conn.close()


def accept_job(root, job_id: str, revision: int, evidence: str) -> str:
    project = load_project(root)
    from . import review_policy, verification
    review = review_policy.mode(project)
    if review == "human":
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
            from .review_policy import GATE_REVIEWER, STAGING_REVIEWER
            for task in required:
                approval = conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
                if (not approval or approval["verdict"] != "PASS" or approval["reviewer"] == STAGING_REVIEWER
                        or (approval["reviewer"] == GATE_REVIEWER and review not in ("once", "off"))
                        or not repo.is_ancestor(work, approval["head_sha"], head)):
                    raise ValueError("AI acceptance requires independent approved commits in the combined result")
            if review == "once":
                final = job_acceptance.latest(conn, job_id)
                if not final or final["verdict"] != "PASS" or final["head_sha"] != head:
                    raise ValueError("the combined result needs a passing final review at its current commit")
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
