"""Deciding what runs next, and launching it safely.

Three checks stand between a READY task and a running worker, and all three must
pass:

1. **capability** â€” does any installed agent satisfy this task kind's
   requirements (Â§3.2)? A HOTSPOT never goes to an agent without strong write
   isolation.
2. **overlap** â€” is its predicted write set disjoint from everything already
   running (Â§7.2)? Uncertainty serialises.
3. **idempotency** â€” can we claim `(task_id, generation)` in `worker_runs`? The
   claim is inserted *before* the process spawns, so a second concurrent
   `agentkit run` loses the race and aborts rather than starting a twin (Â§14).
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    adapters,
    audit,
    db,
    instructions,
    launch_failure,
    overlap,
    processes,
    providers,
    quota,
    repo,
    secrets,
    worktrees,
)
from . import statemachine as sm
from .capabilities import CapabilitySet, load_cache
from .config import ProjectConfig, load_project

WORKER_PROMPT = (
    "The host prepares task inputs, output directories and dependencies before launch. "
    "Never install packages, discover runtimes or repair setup. If a preparation problem "
    "appears, report BLOCKED and stop; host code or the manager handles it. "
    "First obtain agentkit brief for task {task_id}. If ToolSearch exists, call it with "
    "query select:mcp__agentkit__brief, then call brief. If discovery or brief fails, "
    "stop and report the blocker; never browse .ai/tasks.yaml, project.yaml or job history "
    "to infer your assignment. Read only required inputs and existing owned output files. "
    "Missing owned outputs are deliverables to create, not preparation errors. "
    "For Codex on Windows use one literal read per shell call: Get-Content -LiteralPath PATH. "
    "Never combine reads using semicolons, pipelines, expansions or scripts; the guard rejects them. "
    "For Claude use Read. Skills are already included below; do not reread their files. "
    "Follow the brief exactly. Commit via AgentKit task_commit BEFORE gate_run. "
    "Run every check ONLY via AgentKit gate_run, never a direct npm/pytest/build shell command. "
    "Stay inside the owned_paths it gives you; edits outside them are blocked and "
    "audited. If you need a path you do not own, call `lease_request` rather than "
    "working around the block. Run `gate_run` before you stop, then `checkpoint`, "
    "then set status REVIEW with the gate output as evidence."
)


@dataclass
class LaunchPlan:
    task: dict[str, Any]
    adapter_name: str
    worktree: Path
    generation: int
    reason: str
    model_selection: dict | None = None

    def describe(self) -> str:
        return (
            f"task {self.task['id']} ({self.task['title']}) -> {self.adapter_name} "
            f"gen {self.generation} in {self.worktree}"
        )


@dataclass
class SchedulerReport:
    launched: list[LaunchPlan] = field(default_factory=list)
    deferred: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    unassignable: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    parked: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    woken: list[int] = field(default_factory=list)
    cooling: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = []
        for provider, why in sorted(self.cooling.items()):
            lines.append(f"cooling   {provider}: {why}")
        if self.woken:
            lines.append(f"woken     {', '.join(str(t) for t in self.woken)}")
        for plan in self.launched:
            lines.append(f"launched  {plan.describe()}")
        for task, why in self.deferred:
            lines.append(f"deferred  task {task['id']} ({task['title']}): {why}")
        for task, why in self.parked:
            lines.append(f"parked    task {task['id']} ({task['title']}): {why}")
        for task, why in self.unassignable:
            lines.append(f"blocked   task {task['id']} ({task['title']}): {why}")
        return "\n".join(lines) or "nothing to launch"


def choose_adapter(
    task: dict[str, Any],
    capabilities: dict[str, CapabilitySet],
    *,
    unavailable: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Pick an available agent that satisfies this task kind's requirements.

    Nothing here names a vendor: it compares the task's requirements against
    measured capability sets, which is what lets a new agent qualify by probing
    rather than by a code change.

    `unavailable` maps adapter -> why, for accounts currently in cooldown. A
    capable-but-exhausted provider is skipped rather than launched into the same
    limit that just stopped the previous worker. Falling through to a *different*
    provider happens only when that provider independently satisfies the task's
    requirements: capability rules are never relaxed to route around a cooldown.
    """
    kind = str(task.get("kind") or "SAFE_PARALLEL")
    preferred = task.get("adapter")
    cooling = unavailable or {}
    reasons: list[str] = []
    blocked_but_capable: list[str] = []

    ordered = sorted(capabilities.items(), key=lambda kv: (kv[0] != preferred, kv[0]))
    for name, caps in ordered:
        missing = caps.missing_for(kind)
        if missing:
            reasons.append(f"{name} lacks {', '.join(missing)}")
            continue
        if name in cooling:
            blocked_but_capable.append(f"{name} is {cooling[name]}")
            continue
        return name, f"{name} satisfies {kind} requirements"

    if not capabilities:
        return None, "no probed agents; run `agentkit probe`"
    if blocked_but_capable:
        return None, "provider unavailable: " + "; ".join(blocked_but_capable)
    return None, f"no agent satisfies {kind}: " + "; ".join(reasons)


def unavailable_adapters(conn: sqlite3.Connection) -> dict[str, str]:
    """Adapters whose account is in cooldown, with a human-readable reason."""
    out: dict[str, str] = {}
    for state in providers.list_states(conn):
        if state.status == providers.AVAILABLE or state.available_at():
            continue
        minutes = max(1, state.seconds_remaining() // 60)
        out[state.provider] = f"in {state.status.lower()} for ~{minutes} min ({state.reason})"
    return out


def eligible_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ready = db.list_tasks(conn, (sm.READY,))
    active = {p["task_id"] for p in processes.owning(conn)}
    done = {str(t["id"]) for t in db.list_tasks(conn, (sm.DONE,))}
    done.update(str(t["spec_id"]) for t in db.list_tasks(conn, (sm.DONE,)) if t.get("spec_id"))
    return sorted((t for t in ready if t["kind"] != "OPERATOR" and t["id"] not in active
                   and {str(dep) for dep in t["depends_on"]}.issubset(done)),
                  key=lambda t: (int(t["priority"] if t.get("priority") is not None else 100), int(t["id"])))


def plan(
    conn: sqlite3.Connection,
    root: str | Path,
    project: ProjectConfig,
    *,
    max_workers: int | None = None,
) -> tuple[list[LaunchPlan], SchedulerReport]:
    from . import concurrency, manager_state
    report = SchedulerReport()
    if project.raw.get("execution_paused") is True:
        return [], report
    manager_state.capture_current(conn)
    limit = concurrency.resolve(project, max_workers)
    # Providers first: a task whose provider just recovered should be considered
    # in this same pass, not the next one.
    report.woken = quota.wake_ready(conn)
    report.promoted = db.refresh_ready(conn)
    cooling = unavailable_adapters(conn)
    report.cooling = cooling

    running = [t for t in db.list_tasks(conn, (sm.LEASED, sm.RUNNING, sm.VERIFYING))
               if t["kind"] != "OPERATOR"]
    # None means no numeric cap: the batch is bounded by the finite READY set and
    # by the overlap, lease, dependency and resource checks below.
    slots = None if limit is None else max(0, limit - len(running))
    if slots == 0:
        return [], report

    from .workspace_migration import pending
    if pending(root):
        report.deferred.append(({"id": "migration", "title": "Workspace migration"}, "Recover pending migration before launch"))
        return [], report
    capabilities = load_cache(root)
    candidates = [t for t in eligible_tasks(conn) if manager_state.allows_launch(conn, t)]
    candidates = [t for t in candidates if not t.get("job_id") or conn.execute(
        "SELECT 1 FROM jobs WHERE id=? AND status='ACTIVE' AND planned_revision=revision", (t["job_id"],)).fetchone()]
    from .workflow_setup import worker_slots
    caps = worker_slots(project)
    chosen, deferred = overlap.schedulable_set(conn, root, candidates, limit=slots, role_limits=caps)

    for task, why in deferred:
        report.deferred.append((task, why))

    counts = {role: sum(task["role"] == role for task in running) for role in caps}
    plans: list[LaunchPlan] = []
    for task in chosen:
        from .environment_prepare import hold
        try:
            work = worktrees.path_for(root, task, project)
            reason = hold(project, work, task) if work.is_dir() else None
        except (ValueError, OSError) as exc:
            reason = str(exc)
        if reason:
            report.deferred.append((task, "Environment setup blocked: " + reason))
            continue
        role = task["role"]
        if role in caps and counts[role] >= caps[role]:
            report.deferred.append((task, "configured worker role slots are occupied"))
            continue
        from . import models, workflow
        try:
            workflow.validate_task(project, task)
        except ValueError as exc:
            report.unassignable.append((task, str(exc)))
            continue
        available_caps = dict(capabilities)
        if any(p.adapter_name == "local-opencode" for p in plans):
            available_caps.pop("local-opencode", None)
        selected, reason = models.choose_worker(conn, project, task, available_caps, unavailable=cooling)
        adapter_name = selected.provider if selected else None
        if adapter_name is None:
            if reason.startswith("provider unavailable"):
                _park_until_provider(conn, task, cooling, reason, project)
                report.parked.append((task, reason))
            else:
                report.unassignable.append((task, reason))
            continue
        if role in counts:
            counts[role] += 1
        generation = int(task.get("generation") or 0) + 1
        worktree = worktrees.path_for(root, task, project)
        plans.append(
            LaunchPlan(task=task, adapter_name=adapter_name, worktree=worktree,
                       generation=generation, reason=reason, model_selection=selected.to_dict() if selected else None)
        )
    return plans, report


def launch(
    conn: sqlite3.Connection,
    root: str | Path,
    project: ProjectConfig,
    launch_plan: LaunchPlan,
    *,
    dry_run: bool = False,
    detach: bool = True,
) -> tuple[bool, str]:
    """Take the lease, claim the generation, then start the process. In that order."""
    if project.raw.get("execution_paused") is True:
        return False, "execution is paused by the operator"
    task = launch_plan.task
    task_id = int(task["id"])
    current = db.get_task(conn, task_id)
    if not current or current["status"] != "READY" or any(p["task_id"] == task_id for p in processes.owning(conn)):
        return False, "task is no longer ready or already has a session"
    if current.get("job_id") and not conn.execute("SELECT 1 FROM jobs WHERE id=? AND status='ACTIVE' AND revision=planned_revision", (current["job_id"],)).fetchone():
        return False, "job needs coordinator planning before launch"
    from . import manager_state
    manager_state.capture_current(conn)
    if not manager_state.allows_launch(conn, current):
        return False, "new dependent work awaits manager recovery audit"
    task = current
    from . import models
    selected = models.Profile(**launch_plan.model_selection) if launch_plan.model_selection else models.for_launch(
        project, task, launch_plan.adapter_name, str(task.get("role") or "implementer"))
    if not models.usable(conn, project, selected):
        return False, "selected model or account is unavailable"
    if selected not in models.allowed_workers(conn, project, task):
        return False, "task model policy changed since scheduling"
    if selected.provider == "local-opencode" and any(p["provider"] == selected.provider for p in processes.owning(conn)):
        return False, "local GPU already has an active worker"
    task = {**task, "_model_selection": selected.to_dict()}
    adapter = adapters.get(launch_plan.adapter_name, project)
    if adapter is None:
        return False, f"adapter {launch_plan.adapter_name} disappeared"
    runtime_issue = adapter.runtime_problem(selected.model)
    if runtime_issue:
        return False, runtime_issue
    from .capabilities import cached_runtime_problem
    provenance_issue = cached_runtime_problem(root, adapter, task["kind"])
    if provenance_issue:
        return False, provenance_issue

    paths = list(task.get("expected_write") or task.get("owned_paths") or [])
    if not project.gate(task["gate_level"]) or not project.gate("full"):
        return False, "both the task gate and full gate must be configured"
    if task["kind"] not in ("RESEARCH", "REVIEW") and not paths:
        return False, "write tasks require explicit expected_paths.write"
    if task["kind"] in ("RESEARCH", "REVIEW") and paths:
        return False, "read-only task kinds cannot declare write paths"
    if task.get("budget_usd") is not None and task.get("spend_usd", 0) >= task["budget_usd"]:
        return False, "task budget is exhausted; coordinator must revise the budget or scope"
    from . import workflow
    if project.raw.get("execution_paused") is True:
        return False, "execution is paused by the operator"
    workflow.validate_task(project, task)
    from .workflow_setup import worker_slots
    role_caps = worker_slots(project)
    role = task["role"]
    if role in role_caps:
        occupied = [db.get_task(conn, process["task_id"]) for process in processes.owning(conn)
                    if process["purpose"] == "worker" and process["task_id"] != task_id]
        if sum(bool(item and item["role"] == role) for item in occupied) >= role_caps[role]:
            return False, "configured worker role slots are occupied"
    from .run_limits import limits
    limits(project)  # Refuse invalid caps before claiming a generation or lease.
    try:
        prompt = instructions.prompt(task, project, WORKER_PROMPT.format(task_id=task_id))
        workflow.validate_launch_context(conn, project, task, prompt, paths, launch_plan.worktree)
    except ValueError as exc:
        return False, str(exc)

    if dry_run:
        worktree = launch_plan.worktree
        built = adapter.build_launch(
            {**task, "generation": launch_plan.generation}, worktree,
            str(task.get("role") or "implementer"), project, prompt=prompt,
        )
        return True, built.display()

    # 1. Worktree, and the base commit the merge gate will diff against.
    worktree, created = worktrees.ensure(root, task, project)
    from .test_packets import verify_checkout
    try:
        verify_checkout(conn, project, task, worktree)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    base = task.get("base_sha") or repo.head_commit(worktree)
    continuation = None
    if task.get("base_sha"):
        from . import handoff
        continuation = handoff.verify(conn, project, task, worktree, selected)
        if not continuation.ok:
            db.log_event(conn, task_id, "handoff_refused", cause=continuation.reason, detail=continuation.evidence)
            return False, continuation.reason
        db.log_event(conn, task_id, "handoff_audited", cause=continuation.reason,
                     detail={"from": task.get("adapter"), "to": launch_plan.adapter_name, "base": base,
                             "cross_provider": continuation.cross})
        if repo.head_commit(worktree) == base and repo.is_clean(worktree):
            # Audited, but nothing was preserved: start fresh rather than send a
            # packet that only repeats the brief and spends the context budget.
            continuation = None

    try:
        workflow.validate_launch_context(conn, project, task, prompt, paths, worktree)
    except ValueError as exc:
        return False, str(exc)

    from .worker_preparation import prepare as prepare_files
    try:
        prepare_files(conn, project, task, worktree)
    except (ValueError, OSError) as exc:
        db.log_event(conn, task_id, "worker_preparation_blocked", cause=str(exc),
                     effect="host/manager repair required; no worker started")
        return False, str(exc)

    from .environment_prepare import prepare
    setup = prepare(project, worktree, task)
    if not setup.passed:
        return False, setup.summary()
    from .worker_preparation import record_ready
    record_ready(conn, project, task, worktree, setup)
    if continuation is not None:
        prompt += "\n" + handoff.packet(conn, root, worktree, task, continuation)
        try:
            workflow.validate_launch_context(conn, project, task, prompt, paths, worktree)
        except ValueError as exc:
            return False, str(exc)

    # 2. Generation and idempotency claim, before anything spawns.
    from . import launch_claim
    generation, claim_reason = launch_claim.claim(conn, task, launch_plan.reason)
    if generation is None:
        return False, claim_reason
    run_id = db.open_worker_run(
        conn, task_id, generation, launch_plan.adapter_name, str(worktree)
    )
    if run_id is None:
        launch_claim.release_failed(conn, task_id, generation, "duplicate worker claim")
        return False, f"generation {generation} already claimed; another scheduler is running"

    # 3. Lease. Conflict detection happens inside the same transaction as the
    #    insert, so a concurrent scheduler cannot slip between check and grant.
    _granted, clashes = db.try_acquire_leases(
        conn, task_id, paths, mode="exclusive-write", generation=generation
    )
    if clashes:
        db.close_worker_run(conn, run_id, exit_code=None)
        launch_claim.release_failed(conn, task_id, generation, "lease conflict before spawn")
        db.log_event(
            conn, task_id, "lease_conflict",
            cause=f"task {clashes[0]['task_id']} holds {clashes[0]['held']}",
            effect="launch aborted; no state mutated",
            detail={"conflicts": clashes},
        )
        return False, (
            f"lease conflict with task {clashes[0]['task_id']} on {clashes[0]['held']}"
        )
    db.update_task(
        conn, task_id, owned_paths=paths, adapter=launch_plan.adapter_name,
        worktree=str(worktree), base_sha=base,
        branch=repo.current_branch(worktree), blocker=None,
        model=selected.model,
        session_token=workflow.resume_token(project, task, task.get("adapter") == launch_plan.adapter_name and task.get("model") == selected.model),
    )

    # 3b. A worker must not be launched into a worktree that contains credentials:
    #     tool-level deny rules do not survive `cat`, and the strongest available
    #     rule is that the secret should not be reachable at all (Â§5).
    problems = secrets.assert_clean(worktree)
    if problems:
        db.release_leases(conn, task_id, reason="secret pre-flight failed")
        db.close_worker_run(conn, run_id, exit_code=None)
        db.set_status(conn, task_id, sm.READY, actor="scheduler",
                      cause="worktree contains credentials")
        db.log_event(
            conn, task_id, "secret_preflight_failed",
            cause=f"{len(problems)} credential path(s) reachable from the worktree",
            effect="launch refused; no worker started",
            detail={"problems": problems},
        )
        return False, "worktree contains credentials:\n  " + "\n  ".join(problems)

    # 4. Guards, and a record of which layers are actually live.
    orchestrator = Path(__file__).resolve().parents[1]
    report = adapter.install_guards(worktree, {**task, "generation": generation}, orchestrator)
    audit.install_pre_commit(worktree, orchestrator)
    report.active.add("L6_pre_commit")
    if workflow.enabled(project):
        report.active.add("L5_host_monitor")
    db.log_event(
        conn, task_id, "guards_installed",
        cause=f"adapter {launch_plan.adapter_name}",
        effect=f"active: {', '.join(sorted(report.active))}",
        detail=report.to_dict(),
    )

    # 5. Process. A session ID only means something to the provider and model that
    #    issued it; a cross-provider continuation always starts fresh.
    same_session = (not (continuation and continuation.cross) and task.get("adapter") == launch_plan.adapter_name
                    and task.get("model") == selected.model)
    try:
        built = adapter.build_launch(
            {**task, "generation": generation}, worktree,
            str(task.get("role") or "implementer"), project, prompt=prompt,
            resume_token=workflow.resume_token(project, task, same_session),
        )
        # Allowlisted, not inherited: a worker must not receive whatever cloud or
        # registry credentials happen to be exported in the launching shell (Â§5).
        built.env = {**worktrees.shared_cache_env(project.root), **built.env}
        if workflow.enabled(project):
            built.env["AGENTKIT_AUDIT_OWNER"] = "monitor"
        db.heartbeat(conn, task_id, generation)
        db.set_status(conn, task_id, sm.RUNNING, actor="scheduler", cause="supervised worker starting")
        if continuation is not None and continuation.cross:
            from . import handoff
            with db.immediate_transaction(conn):
                handoff.record(conn, task, continuation, generation)
        process_id = processes.start(conn, root, built, purpose="worker", provider=launch_plan.adapter_name,
            task_id=task_id, job_id=task.get("job_id"), generation=generation, worker_run_id=run_id)
        process = processes.get(conn, process_id)
        assert process is not None
    except (OSError, ValueError) as exc:
        db.close_worker_run(conn, run_id, exit_code=127)
        db.release_leases(conn, task_id, reason="launch failed")
        db.set_status(conn, task_id, sm.READY, actor="scheduler", cause=f"launch failed: {exc}")
        return False, f"failed to launch: {exc}"

    db.set_worker_pid(conn, run_id, process["pid"])
    db.log_event(
        conn, task_id, "worker_launched",
        cause=launch_plan.reason,
        effect=f"pid {process['pid']} in {worktree}",
        detail={
            "adapter": launch_plan.adapter_name, "generation": generation,
            "model": selected.model, "profile": selected.name, "effort": selected.effort,
            "worktree": str(worktree), "worktree_created": created,
            "base_sha": base, "guards": sorted(report.active),
            "lease_paths": paths,
        },
    )
    return True, f"pid {process['pid']} (monitor {process_id})"


def _lease_conflicts(
    conn: sqlite3.Connection, paths: list[str], task_id: int
) -> list[dict[str, Any]]:
    from .leases import conflicts

    return [c for c in conflicts(conn, paths, task_id) if c["task_id"] != task_id]


def run_once(
    root: str | Path, *, max_workers: int | None = None, dry_run: bool = False
) -> SchedulerReport:
    from .locking import exclusive
    with exclusive(root, "scheduler"):
        return _run_once(root, max_workers=max_workers, dry_run=dry_run)


def _run_once(root, *, max_workers=None, dry_run=False):
    root_path = Path(root)
    project = load_project(root_path)
    conn = db.connect(root_path)
    try:
        plans, report = plan(conn, root_path, project, max_workers=max_workers)
        if not dry_run:
            for task, reason in report.unassignable:
                launch_failure.record(conn, task["id"], reason)
        for launch_plan in plans:
            ok, detail = launch(conn, root_path, project, launch_plan, dry_run=dry_run)
            if ok:
                launch_plan.reason = f"{launch_plan.reason} ({detail})" if not dry_run else detail
                report.launched.append(launch_plan)
            else:
                report.unassignable.append((launch_plan.task, detail))
                if not dry_run:
                    launch_failure.record(conn, launch_plan.task["id"], detail)
        return report
    finally:
        conn.close()


def _park_until_provider(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    cooling: dict[str, str],
    reason: str,
    project: ProjectConfig | None = None,
) -> None:
    """Block a READY task whose only capable provider is exhausted.

    Without this the scheduler would re-evaluate the same task every pass and log
    the same refusal forever. Parking makes the wait explicit and visible, and
    `quota.wake_ready` brings the task back automatically when the account
    recovers.
    """
    from . import models
    # Park on the account the task's allowed models actually need, not on whichever
    # provider ran it last: a pinned task must not wake for an unrelated account.
    waiting = models.waiting_provider(conn, project, task, cooling) if project is not None else None
    provider = str(waiting or task.get("adapter") or next(iter(cooling), ""))
    state = providers.get_state(conn, provider) if provider else None
    quota.pause(
        conn, int(task["id"]), provider=provider,
        account=state.account if state else "default",
        retry_at=state.retry_at if state else None,
        raw_message=reason,
    )
