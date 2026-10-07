"""The AgentKit MCP server â€” the only way an agent touches shared state.

Claude Code and Codex both speak MCP, so the same server gives both of them
identical semantics for "what am I working on", "may I edit this", "save my
progress" and "did the tests pass". That is what lets a task be started by one
and finished by the other.

Every tool resolves its task id the same way: an explicit argument wins,
otherwise `AGENTKIT_TASK`, otherwise the worktree name.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# mcp >= 2.0 renamed FastMCP to MCPServer; the decorator API is unchanged.
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[attr-defined,no-redef]

from . import (
    audit,
    briefs,
    db,
    gates,
    hotspots,
    jobs,
    leases,
    planning,
    processes,
    repo,
    reviews,
    spec,
    worker,
)
from .config import load_project
from .context import active_task_id
from .mcp_extra import READ_ONLY
from .paths import find_project_root, resolve_within

mcp = _Server(
    "agentkit",
    instructions=(
        "Shared task graph and file-ownership enforcement for multi-agent work. "
        "Call `brief` first in any worker session. Call `lease_check` before editing "
        "a file you are not certain you own. Never edit .ai/tasks.db directly."
    ),
)


def _root() -> Path:
    root = find_project_root(os.environ.get("AGENTKIT_ROOT") or os.getcwd())
    if root is None:
        raise RuntimeError(
            "No project found. Run `agentkit init` in the repository first, "
            "or set AGENTKIT_ROOT."
        )
    return root


def _resolve_task(task_id: int | None) -> int | None:
    return task_id if task_id is not None else active_task_id()


def _needs_task(task_id: int | None) -> int:
    resolved = _resolve_task(task_id)
    if resolved is None:
        raise RuntimeError(
            "No active task. Pass task_id explicitly, or launch this session through "
            "`agentkit run` so AGENTKIT_TASK is set."
        )
    return resolved


def _mutating_task(task_id: int | None) -> int:
    """Resolve the task and refuse if this worker has been superseded (Â§6).

    Every worker-originated mutation goes through here. Read-only tools use
    `_needs_task` instead, so a stale worker can still ask what happened to it.
    """
    resolved = _needs_task(task_id)
    if os.environ.get("AGENTKIT_ROLE") in ("review", "coordinator"):
        raise PermissionError("control sessions cannot report progress as an implementer")
    root = _root()
    conn = db.connect(root)
    try:
        worker.require_current(conn, resolved)
    finally:
        conn.close()
    return resolved


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- briefs


@mcp.tool(annotations=READ_ONLY)
def brief(task_id: int | None = None) -> str:
    """Get everything needed to start or resume a task: goal, scope, gates, last checkpoint.

    Call this first in any worker session, before reading code.
    """
    root = _root()
    assigned = os.environ.get("AGENTKIT_TASK", "").strip()
    if task_id is not None and assigned.isdigit() and int(assigned) != task_id:
        # A session launched for one task must not adopt another task's scope.
        return (f"Refused: this session is assigned task {assigned} and may read only that "
                "brief. Call brief with no task_id.")
    resolved = _needs_task(task_id)
    conn = db.connect_readonly(root)
    try:
        data = briefs.build(conn, load_project(root), resolved)
        if data is None:
            return f"Task {resolved} does not exist. Use task_list to see what does."
        return briefs.render(data)
    finally:
        conn.close()


@mcp.tool(annotations=READ_ONLY)
def task_list(status: str | None = None) -> str:
    """List tasks in the graph, optionally filtered by status (READY, RUNNING, DONE...)."""
    root = _root()
    conn = db.connect_readonly(root)
    try:
        tasks = db.list_tasks(conn, (status,) if status else None)
        if not tasks:
            return "No tasks yet. Use /plan or task_create to build the graph."
        rows = [
            {
                "id": t["id"],
                "title": t["title"],
                "kind": t["kind"],
                "status": t["status"],
                "role": t["role"],
                "depends_on": t["depends_on"],
                "owned_paths": t["owned_paths"],
            }
            for t in tasks
        ]
        return _dump(rows)
    finally:
        conn.close()


@mcp.tool()
def task_create(
    title: str,
    description: str = "",
    kind: str = "SAFE_PARALLEL",
    role: str = "implementer",
    owned_paths: list[str] | None = None,
    depends_on: list[int] | None = None,
    gate_level: str = "fast",
    priority: int = 100,
) -> str:
    """Add a task to the graph. Used by the architect when planning a feature package.

    `kind` is one of SAFE_PARALLEL, DEPENDENT, HOTSPOT, DECOUPLE, TEST_ONLY, RESEARCH.
    `owned_paths` are globs this task alone may edit â€” the narrower, the more agents
    can run at once.
    """
    root = _root()
    if kind not in db.TASK_KINDS:
        return f"Invalid kind '{kind}'. Use one of: {', '.join(db.TASK_KINDS)}"
    control = _require_planner()
    task_id = planning.create(root, title=title, description=description, kind=kind, role=role,
        expected_write=owned_paths or [], depends_on=depends_on, gate_level=gate_level,
        priority=priority, job_id=(control or {}).get("job_id"))
    return _dump({"task_id": task_id, "status": "saved to tasks.yaml and reconciled"})


# ---------------------------------------------------------------- ownership


@mcp.tool(annotations=READ_ONLY)
def lease_check(path: str, task_id: int | None = None) -> str:
    """Ask whether this task may edit a path, and why not if it may not.

    Agents whose probe reports `prewrite_file_guard` have this enforced
    automatically before every write. Call it explicitly when you are not certain
    you own a path â€” being told "no" costs nothing, and an out-of-lease change is
    rejected at the merge gate regardless of how it got there.
    """
    root = _root()
    resolved = _resolve_task(task_id)
    rel = resolve_within(path, root)
    if rel is None:
        return _dump({"allowed": False, "reason": f"`{path}` is outside the repository.",
                      "code": "outside_repo"})
    conn = db.connect_readonly(root)
    try:
        verdict = leases.decide(conn, load_project(root), rel, resolved)
        return _dump({"path": rel, **verdict.to_dict()})
    finally:
        conn.close()


@mcp.tool()
def lease_acquire(paths: list[str], task_id: int | None = None, exclusive: bool = True) -> str:
    """Claim paths for this task. Fails if another live task already holds them."""
    root = _root()
    resolved = _mutating_task(task_id)
    conn = db.connect(root)
    try:
        mode = "exclusive-write" if exclusive else "shared-read"
        task = db.get_task(conn, resolved) or {}
        if exclusive and any(p not in (task.get("expected_write") or task.get("owned_paths") or []) for p in paths):
            raise PermissionError("scope expansion requires a coordinator-approved graph amendment")
        acquired, clashes = db.try_acquire_leases(conn, resolved, paths, mode=mode)
        if clashes:
            return _dump({"acquired": [], "blocked_by": clashes})
        task = db.get_task(conn, resolved) or {}
        merged = sorted({*(task.get("owned_paths") or []), *paths})
        db.update_task(conn, resolved, owned_paths=merged)
        return _dump({"acquired": acquired, "owned_paths": merged})
    finally:
        conn.close()


@mcp.tool()
def lease_request(paths: list[str], reason: str, task_id: int | None = None) -> str:
    """Ask to widen your scope to paths you do not own.

    Use this instead of editing a file you were blocked from. It records a
    proposal for the architect rather than silently expanding the task, which is
    what keeps concurrent agents from colliding.
    """
    root = _root()
    resolved = _resolve_task(task_id)
    conn = db.connect(root)
    try:
        if resolved is not None:
            worker.require_current(conn, resolved)
        clashes = leases.conflicts(conn, list(paths))
        proposal = f"widen task {resolved} scope to: {', '.join(paths)}"
        amendment_id = db.create_amendment(conn, resolved, proposal, reason)
        return _dump(
            {
                "amendment_id": amendment_id,
                "status": "OPEN",
                "conflicts": clashes,
                "next": "Stop work on those paths. Continue with what you do own, or "
                        "report that you are blocked so the architect can re-plan.",
            }
        )
    finally:
        conn.close()


@mcp.tool(annotations=READ_ONLY)
def conflict_check(paths: list[str]) -> str:
    """Would these paths collide with any live lease? Check before planning parallel work."""
    root = _root()
    conn = db.connect_readonly(root)
    try:
        return _dump({"conflicts": leases.conflicts(conn, list(paths))})
    finally:
        conn.close()


@mcp.tool()
def graph_amend(proposal: str, rationale: str = "", task_id: int | None = None) -> str:
    """Propose a change to the task graph â€” a split, a new dependency, a re-scope.

    The escape hatch for "this task is wrong". Always preferable to quietly doing
    something other than what the task says.
    """
    root = _root()
    resolved = _resolve_task(task_id)
    conn = db.connect(root)
    try:
        if resolved is not None:
            worker.require_current(conn, resolved)
        amendment_id = db.create_amendment(conn, resolved, proposal, rationale)
        return _dump({"amendment_id": amendment_id, "status": "OPEN"})
    finally:
        conn.close()


# ---------------------------------------------------------------- progress


@mcp.tool()
def checkpoint(
    completed: list[str] | None = None,
    remaining: list[str] | None = None,
    next_action: str = "",
    decisions: list[str] | None = None,
    task_id: int | None = None,
) -> str:
    """Save progress so a replacement session can continue without your context.

    Hooks checkpoint automatically before compaction and on stop, but those only
    capture git state. Call this yourself to record *decisions* and *intent* â€”
    the things a diff cannot show.
    """
    root = _root()
    resolved = _mutating_task(task_id)
    conn = db.connect(root)
    try:
        task = db.get_task(conn, resolved)
        if task is None:
            raise ValueError("task not found")
        work = task.get("worktree") or os.getcwd()
        last_commit = repo.head_commit(work)
        payload: dict[str, Any] = {
            "completed": list(completed or []),
            "remaining": list(remaining or []),
            "next_action": next_action,
            "decisions": list(decisions or []),
            "files_changed": repo.changed_files(work),
            "last_commit": last_commit,
            "branch": repo.current_branch(work),
        }
        checkpoint_id = db.write_checkpoint(
            conn, resolved, payload, kind="semantic", reason="agent",
            head_sha=last_commit,
        )
        db.update_task(conn, resolved, next_action=next_action or None,
                       last_commit=last_commit or None)
        db.heartbeat(conn, resolved)
        return _dump({"checkpoint_id": checkpoint_id, "saved": True})
    finally:
        conn.close()


@mcp.tool()
def task_status(status: str, evidence: str = "", task_id: int | None = None) -> str:
    """Move a task to a new state. REVIEW and DONE require evidence (gate output).

    An agent may set: RUNNING, VERIFYING, REVIEW, BLOCKED, FAILED. It may not
    requeue itself, replan itself, or declare its own work integration-ready â€”
    the state machine refuses those and says so.
    """
    root = _root()
    resolved = _mutating_task(task_id)
    upper = status.upper()
    if upper not in db.TASK_STATES:
        return f"Invalid status '{status}'. Use one of: {', '.join(db.TASK_STATES)}"
    if upper in ("REVIEW", "DONE") and not evidence.strip():
        return (
            f"Refusing to set {upper} without evidence. Run the gate first and pass its "
            "summary as `evidence` â€” a task is not done because it feels done."
        )
    conn = db.connect(root)
    try:
        from . import statemachine as sm

        if upper == "REVIEW":
            task = db.get_task(conn, resolved)
            if task is None:
                raise ValueError("task not found")
            work = task.get("worktree") or os.getcwd()
            head = repo.head_commit(work)
            cached = db.cached_gate(conn, resolved, task["gate_level"], head)
            if not cached or not cached["passed"] or not repo.is_clean(work):
                return "Refused: commit changes and pass the task gate at the current HEAD first."
            if not audit.audit_worktree(conn, load_project(root), work, resolved, record=False).clean:
                return "Refused: task has out-of-scope changes."
            if task["status"] == sm.RUNNING:
                db.set_status(conn, resolved, sm.VERIFYING, actor="agent", cause="gate evidence recorded")

        try:
            db.set_status(conn, resolved, upper, actor="agent", evidence=evidence)
        except sm.TransitionError as exc:
            return f"Refused: {exc}"
        if upper in (sm.DONE, sm.FAILED):
            db.release_leases(conn, resolved, reason=f"task {upper}")
            promoted = db.refresh_ready(conn)
            return _dump({"task": resolved, "status": upper, "unblocked_tasks": promoted})
        return _dump({"task": resolved, "status": upper})
    finally:
        conn.close()


@mcp.tool()
def gate_run(level: str | None = None, task_id: int | None = None) -> str:
    """Run this project's declared checks (`fast`, `full`, `types`, `build`...).

    Omit level to use the assigned task gate, or fast outside a task.
    Commands come from `.ai/project.yaml`, so this works the same way in a Python
    service, a React app or a C++ plugin.
    """
    root = _root()
    project = load_project(root)
    resolved = _resolve_task(task_id)
    work = os.getcwd()
    if resolved is not None:
        conn = db.connect(root)
        try:
            task = db.get_task(conn, resolved)
            if task is None:
                raise ValueError("task not found")
            if not task:
                raise ValueError("unknown task")
            if os.environ.get("AGENTKIT_ROLE") == "review":
                processes.require_control(conn, purposes=("review",), task_id=resolved)
            else:
                worker.require_current(conn, resolved)
            from .workflow import validate_gate
            level = level if level is not None else task["gate_level"]
            validate_gate(project, task, level)
            work = task.get("worktree") or work
        finally:
            conn.close()
    head = repo.head_commit(work)
    clean = repo.is_clean(work)
    level = level if level is not None else "fast"
    result = gates.run_gate(project, level, cwd=work)
    if resolved is not None:
        conn = db.connect(root)
        try:
            worker.require_current(conn, resolved)
            db.log_event(conn, resolved, "gate_run",
                         cause=f"level={level}",
                         effect="passed" if result.passed else "failed",
                         detail={"level": level, "passed": result.passed})
            db.record_gate(conn, resolved, level, head,
                           result.passed and clean and repo.is_clean(work) and repo.head_commit(work) == head, result.summary())
            if not result.passed:
                task = db.get_task(conn, resolved) or {}
                db.update_task(conn, resolved, attempts=int(task.get("attempts") or 0) + 1)
        finally:
            conn.close()
    return result.summary()


@mcp.tool(annotations=READ_ONLY)
def gate_list() -> str:
    """Show which gate commands this project declares."""
    return gates.describe_gates(load_project(_root()))


# ---------------------------------------------------------------- analysis


@mcp.tool(annotations=READ_ONLY)
def hotspot_report(limit: int = 15, days: int = 90) -> str:
    """Rank files that force agents to work one at a time.

    Use this before planning parallel work: the top entries are the files that
    need decoupling first, in order.
    """
    root = _root()
    spots = hotspots.analyse(root, days=days, limit=limit)
    return hotspots.format_table(spots, root)


@mcp.tool()
def audit_diff(base: str = "HEAD", task_id: int | None = None) -> str:
    """Check that everything this branch changed falls inside the task's lease.

    The pre-merge equivalent of the PreToolUse hook â€” this is how Codex work gets
    the same guarantee Claude gets before the write happens.
    """
    root = _root()
    resolved = _needs_task(task_id)
    project = load_project(root)
    conn = db.connect(root)
    try:
        task = db.get_task(conn, resolved)
        if task is None:
            raise ValueError("task not found")
        work = task.get("worktree") or os.getcwd()
        return _dump(audit.audit_worktree(conn, project, work, resolved, record=False).to_dict())
    finally:
        conn.close()


def _require_planner(job_id=None):
    if os.environ.get("AGENTKIT_PROCESS"):
        conn = db.connect(_root())
        try:
            return processes.require_control(conn, purposes=("coordinator",), job_id=job_id)
        finally:
            conn.close()
    if active_task_id() is not None:
        raise PermissionError("workers propose graph_amend; only the coordinator edits the plan")
    if job_id:
        from . import manager
        from .mcp_manager import credential
        root = _root()
        conn = db.connect(root)
        try:
            if manager.blocks_spawn(conn, job_id):
                token = credential(root, job_id)
                if not token:
                    raise PermissionError("external manager lease owns this job")
                return manager.require_lease(conn, job_id, token)
        finally:
            conn.close()
    return None  # Interactive operator/coordinator connection.


@mcp.tool()
def job_start(max_workers: int | None = None) -> str:
    """Start the persistent supervisor; it survives this agent session ending.

    Omit max_workers to use the project's setting (default 0 = every eligible
    independent task); a positive number is an optional cap.
    """
    _require_planner()
    from .service import start
    return start(_root(), max_workers)


@mcp.tool()
def project_configure(gates: dict[str, list[str]], worktree_setup: list[str],
                      hot_paths: list[str], contracts: list[str]) -> str:
    """Coordinator sets commands and boundaries after inspecting the current repository."""
    _require_planner()
    import yaml

    from .locking import atomic_write, exclusive
    root = _root()
    conn = db.connect(root)
    try:
        if any(p["purpose"] != "coordinator" for p in processes.active(conn)):
            raise ValueError("wait for active workers and reviews before changing project policy")
    finally:
        conn.close()
    if not gates.get("full") or not all(isinstance(v, list) and all(isinstance(c, str) and c.strip() for c in v) for v in gates.values()):
        raise ValueError("gates must contain nonempty command lists, including full")
    with exclusive(root, "project"):
        path = root / ".ai" / "project.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.update(gates=gates, worktree_setup=worktree_setup, hot_paths=hot_paths, contracts=contracts)
        atomic_write(path, yaml.safe_dump(data, sort_keys=False))
    return "Project configuration saved."


@mcp.tool()
def job_create(job_id: str, request: str, coordinator: str = "auto", acceptance: list[str] | None = None, reviewers: list[str] | None = None) -> str:
    """Persist the user's original request before planning. Coordinator stays pinned."""
    _require_planner()
    return _dump(jobs.create(_root(), job_id, request, coordinator, acceptance=acceptance, reviewers=reviewers))


@mcp.tool(annotations=READ_ONLY)
def job_brief(job_id: str, include_history: bool = False, offset: int = 0, limit: int = 20) -> str:
    """Compact job intent and paged unfinished tasks; opt in to complete history.

    User requests and acceptance remain complete. Operational decision/blocker
    previews disclose omitted characters; include_history=True returns full evidence.
    """
    root = _root()
    conn = db.connect_readonly(root)
    try:
        if not include_history:
            from .compact_jobs import summary
            return _dump(summary(conn, root, job_id, offset=offset, limit=limit))
        from .models import catalog
        from .policy import describe
        project = load_project(root)
        return jobs.packet(root, job_id) + "\n" + _dump({
            "model_policy": catalog(project), "model_pins": describe(project),
            "tasks": [t for t in db.list_tasks(conn) if t.get("job_id") == job_id],
            "amendments": db.list_amendments(conn),
            "runtime": dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()),
            "manager": __import__("agentkit.manager", fromlist=["packet"]).packet(conn, job_id)})
    finally:
        conn.close()


@mcp.tool()
def job_decision(job_id: str, decision: str) -> str:
    """Append a durable coordinator decision without overwriting user intent."""
    _require_planner(job_id)
    job = jobs.amend(_root(), job_id, decision)
    return _dump({"id": job["id"], "revision": job["revision"],
                  "decision_count": len(job["decisions"])})


@mcp.tool(annotations=READ_ONLY)
def task_diff(task_id: int, offset: int = 0, limit: int = 60000) -> str:
    """Read a page of the committed diff without enabling a reviewer's shell."""
    from .mcp_extra import source_diff
    return source_diff(task_id=task_id, offset=offset, limit=limit)


@mcp.tool()
def task_define(definition: dict[str, Any]) -> str:
    """Create or revise a task using the tasks.yaml schema, including job_id and skills."""
    task = spec.TaskSpec.from_dict(definition)
    if os.environ.get("AGENTKIT_PROCESS") and not task.job_id:
        raise ValueError("supervised planning requires a job_id")
    _require_planner(task.job_id)
    if task.job_id:
        jobs.load(_root(), task.job_id)
    return _dump({"task_id": planning.put(_root(), task)})


@mcp.tool()
def job_plan_ready(job_id: str, revision: int, summary: str) -> str:
    """Activate a graph that covers this exact revision of the user's request."""
    _require_planner(job_id)
    root = _root()
    job = jobs.load(root, job_id)
    if revision != job["revision"]:
        raise ValueError("user intent changed; reread job_brief")
    conn = db.connect(root)
    try:
        from . import manager_state
        manager_state.require_clear(conn, job_id)
        tasks = [t for t in db.list_tasks(conn) if t.get("job_id") == job_id]
        if not tasks:
            raise ValueError("a job needs a task graph before activation")
        project = load_project(root)
        from .policy import for_task, validate_project
        problems = validate_project(project)
        if problems:
            raise ValueError("model_policy is invalid: " + "; ".join(problems))
        from .run_limits import limits
        from .workflow import validate_task
        limits(project)
        from .review_policy import validate_graph
        from .workflow_setup import worker_slots
        worker_slots(project)
        validate_graph(project, tasks)
        for task in tasks:
            if task["status"] in ("DONE", "CANCELLED"):
                continue  # Historical scopes do not create fresh assignments.
            validate_task(project, task)
            from .test_packets import dependencies
            dependencies(project, task, tasks)
            from .instructions import prompt
            prompt(task, project, "Validate role and skill instructions")
            for_task(project, task)
            if not project.gate(task["gate_level"]):
                raise ValueError(f"undefined gate {task['gate_level']}")
        if not project.gate("full"):
            raise ValueError("full gate is required")
        conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=?,last_error=NULL,updated_at=? WHERE id=?", (revision, db.utcnow(), job_id))
    finally:
        conn.close()
    jobs.amend(root, job_id, "Plan activated: " + summary)
    return "Plan active; supervisor will schedule eligible tasks."


@mcp.tool()
def job_block(job_id: str, question: str) -> str:
    """Record a genuine missing user decision. Never use this for provider quotas."""
    _require_planner(job_id)
    conn = db.connect(_root())
    try:
        conn.execute("UPDATE jobs SET status='BLOCKED',last_error=? WHERE id=?", (question, job_id))
    finally:
        conn.close()
    return question


@mcp.tool()
def integration_retry(task_id: int, evidence: str, after_tasks: list[int] | None = None) -> str:
    """Planner retries failed combined checks on unchanged approved source, without a model."""
    root = _root()
    conn = db.connect(root)
    try:
        task = db.get_task(conn, task_id)
        _require_planner(task.get("job_id") if task else None)
        from .integration_retry import authorize
        return authorize(conn, load_project(root), task_id, evidence, after_tasks=after_tasks)
    finally:
        conn.close()


@mcp.tool()
def task_requeue(task_id: int, instructions: str) -> str:
    """Coordinator authorizes a bounded retry after inspecting preserved work."""
    conn = db.connect(_root())
    try:
        task = db.get_task(conn, task_id)
        if task is None:
            raise ValueError("task not found")
        _require_planner(task.get("job_id"))
        if any(p["task_id"] == task_id for p in processes.owning(conn)):
            raise ValueError("previous session still owns this task; prove monitor and child stopped")
        from . import recovery_runtime
        held_ready = task["status"] == "READY" and recovery_runtime.pending(conn, task_id=task_id)
        if task["status"] not in ("FAILED", "STALE", "NEEDS_REPLAN", "BLOCKED") and not held_ready:
            raise ValueError("task does not need recovery")
        if not instructions.strip() or len(instructions) > 2000:
            raise ValueError("retry needs 1-2000 characters of planner instructions")
        recovery_runtime.supersede_worker_intents(conn, task_id, instructions)
        db.bump_generation(conn, task_id)
        db.update_task(conn, task_id, next_action=instructions, blocker=None, blocked_meta=None, attempts=0)
        db.set_status(conn, task_id, "READY", actor="scheduler", cause="coordinator authorized recovery")
        return "Task requeued with preserved work."
    finally:
        conn.close()


@mcp.tool()
def job_accept(job_id: str, revision: int, evidence: str) -> str:
    """Coordinator verifies all job acceptance criteria after every task is merged."""
    _require_planner(job_id)
    from .supervisor import accept_job
    return _dump({"job": job_id, "status": "DONE", "head": accept_job(_root(), job_id, revision, evidence)})


@mcp.tool()
def amendment_resolve(amendment_id: int, decision: str) -> str:
    """Close an amendment after the coordinator has updated the plan or rejected it."""
    _require_planner()
    conn = db.connect(_root())
    try:
        db.resolve_amendment(conn, amendment_id, "RESOLVED")
        db.log_event(conn, None, "amendment_resolved", cause=decision, detail={"amendment": amendment_id})
    finally:
        conn.close()
    return "Resolved."


@mcp.tool()
def review_submit(task_id: int, head_sha: str, verdict: str, evidence: str) -> str:
    """Independent review of the exact commit assigned by the supervisor."""
    root = _root()
    conn = db.connect(root)
    try:
        from .external_review import enabled, submit
        project = load_project(root)
        from .review_policy import mode
        if mode(project) == "human":
            raise PermissionError("human mode uses mechanical staging and operator feature acceptance, not AI review_submit")
        if enabled(project):
            if os.environ.get("AGENTKIT_PROCESS"):
                raise PermissionError("external-manager review requires the attached native manager session")
            submit(conn, project, task_id, head_sha, verdict, evidence)
            return "External manager review recorded."
        process = processes.require_control(conn, purposes=("review", "coordinator"), task_id=task_id)
        task = db.get_task(conn, task_id)
        if task is None:
            raise ValueError("task not found")
        if process["job_id"] != task.get("job_id"):
            raise PermissionError("review belongs to another job")
        if process["purpose"] == "review" and head_sha != process["expected_head"]:
            raise ValueError("reviewed commit changed")
        reviews.ensure_independent(conn, task, process)
        reviews.approve(conn, project, task_id, head_sha, verdict.upper(),
                        f"{process['provider']}:process-{process['id']}", evidence)
        return "Review recorded."
    finally:
        conn.close()


# Read-only source and manager tools live in a separate module to keep this file small.
from .mcp_extra import register as _register_extra  # noqa: E402

_register_extra(mcp)
from .mcp_manager import register as _register_manager  # noqa: E402

_register_manager(mcp)
from .mcp_workspaces import register as _register_workspaces  # noqa: E402

_register_workspaces(mcp)


def main() -> int:
    try:
        mcp.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        sys.stderr.write(f"[agentkit-mcp] fatal: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
