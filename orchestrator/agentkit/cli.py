"""`agentkit` — the human-facing command line.

Agents use MCP tools; you use this. The two share every code path underneath, so
what you see in `agentkit status` is exactly what an agent sees through `brief`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import (
    adapters,
    audit,
    briefs,
    concurrency,
    db,
    gates,
    hotspots,
    init_project,
    integrator,
    jobs,
    leases,
    models,
    observability,
    operator,
    planning,
    probe,
    providers,
    quota,
    reconcile,
    recovery,
    repo,
    reviews,
    scheduler,
    secrets,
    supervisor,
    watch,
)
from . import statemachine as sm
from .capabilities import load_cache as load_capability_cache
from .config import load_project
from .console import use_utf8
from .context import active_task_id
from .paths import find_project_root


def _root_or_exit(explicit: str | None = None) -> Path:
    root = find_project_root(explicit or os.getcwd())
    if root is None:
        sys.stderr.write(
            "Not inside a project. Run this from a git repository, "
            "or run `agentkit init` first.\n"
        )
        raise SystemExit(2)
    return root


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or os.getcwd()).resolve()
    if not (root / ".git").exists():
        sys.stderr.write(f"warning: {root} is not a git repository; worktrees will not work.\n")
    result = init_project.init(root, force=args.force)
    print(f"AgentKit initialised in {root}")
    print(f"  stacks detected: {', '.join(result['stacks']) or 'none'}")
    for rel in result["created"]:
        print(f"  + {rel}")
    for rel in result["skipped"]:
        print(f"  = {rel} (exists, left alone)")
    if result["gitignore"]:
        print(f"  + .gitignore entries: {', '.join(result['gitignore'])}")
    if result["notes"]:
        print("\nNext:")
        for note in result["notes"]:
            print(f"  - {note}")
    print("\nThen: fill in the TODOs in AGENTS.md, run `agentkit hotspots`, "
          "and record hot_paths in .ai/project.yaml.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    project = load_project(root)
    conn = db.connect(root)
    try:
        print(f"project: {project.name or root.name}   root: {root}")
        print(f"stacks: {', '.join(project.stacks) or 'unset'}   "
              f"gates: {', '.join(project.gates) or 'none declared'}")
        active = active_task_id()
        print(f"active task in this shell: {active if active is not None else '-'}")
        print()
        print(observability.render_status(conn))
        if not repo.is_clean(root):
            print(f"\nworking tree: {len(repo.changed_files(root))} uncommitted file(s)")
    finally:
        conn.close()
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    """The four questions from §15.2, each answerable from the event log alone."""
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        answers = {
            "launched": observability.why_launched,
            "stopped": observability.why_stopped,
            "serialized": observability.why_serialized,
            "merge-failed": observability.why_merge_failed,
        }
        print(answers[args.question](conn, args.task))
    finally:
        conn.close()
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        print(observability.render_events(conn, args.task, args.limit))
    finally:
        conn.close()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    if args.rebuild:
        report = reconcile.rebuild_from_git(root)
    else:
        report = reconcile.reconcile(root, adopt_running=args.adopt)
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    if args.watch:
        state = watch.run(
            root, max_workers=args.max_workers, poll_seconds=args.poll,
            dry_run=args.dry_run,
        )
        print(watch.describe(state))
        return 0
    for note in supervisor.tick(root, dry_run=args.dry_run):
        print(note)
    reconcile.reconcile(root, adopt_running=True)
    report = scheduler.run_once(root, max_workers=args.max_workers, dry_run=args.dry_run)
    print(report.summary())
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """Inspect and override provider/account availability."""
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        if args.provider_command == "clear":
            providers.clear_cooldown(conn, args.provider, args.account,
                                     reason="cleared by operator")
            woken = quota.wake_ready(conn)
            print(f"{args.provider} marked available.")
            if woken:
                print(f"Resumed: {', '.join(str(w) for w in woken)}")
        elif args.provider_command == "check":
            names = [args.provider] if args.provider else [a.name for a, _ in adapters.installed(load_project(root))]
            for name in names:
                adapter = adapters.get(name, load_project(root))
                if not adapter:
                    raise ValueError(f"unknown provider {name}")
                state = providers.observe(conn, name, adapter.check_availability())
                print(state.describe())
            for row in conn.execute("SELECT * FROM quota_windows ORDER BY account_key,bucket,window"):
                print(f"  {row['account_key']} {row['bucket']}/{row['window']}: "
                      f"{row['used_percent']}% resets {row['resets_at'] or 'unknown'} (observed {row['observed_at']})")
        elif args.provider_command == "cooldown":
            state = providers.begin_cooldown(
                conn, args.provider, reason=args.reason or "set by operator",
                account=args.account,
            )
            print(state.describe())
        else:
            print(observability.render_providers(conn))
    finally:
        conn.close()
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    outcomes = integrator.run_queue(root, limit=args.limit,
                                    run_full_gate=not args.skip_gate)
    if not outcomes:
        print("Nothing in INTEGRATION_READY.")
        return 0
    for outcome in outcomes:
        print(outcome.summary())
    return 0 if all(o.ok for o in outcomes) else 1


def cmd_adopt(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        print(recovery.adopt(conn, args.task))
    finally:
        conn.close()
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        print(recovery.discard(conn, root, args.task))
    finally:
        conn.close()
    return 0


def cmd_operator(args: argparse.Namespace) -> int:
    """Claim paths for manual editing, through the same lease machinery workers use."""
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        if args.operator_command == "acquire":
            result = operator.acquire(conn, args.paths, reason=args.reason or "")
        elif args.operator_command == "release":
            result = operator.release(conn)
        else:
            result = operator.status(conn)
        print(result.summary())
        return 0 if result.ok else 1
    finally:
        conn.close()


def cmd_scan_secrets(args: argparse.Namespace) -> int:
    """Report credentials reachable from a worktree (§5)."""
    root = _root_or_exit(args.path)
    target = Path(args.worktree) if args.worktree else root
    problems = secrets.assert_clean(target)
    if not problems:
        print(f"No credential files or escaping links found under {target}.")
        return 0
    print(f"{len(problems)} problem(s) under {target}:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


def cmd_audit(args: argparse.Namespace) -> int:
    """Run the L5 audit by hand — useful when you suspect a bypass."""
    root = _root_or_exit(args.path)
    project = load_project(root)
    conn = db.connect(root)
    try:
        task_id = args.task or active_task_id()
        target = args.worktree or root
        result = audit.audit_worktree(conn, project, target, task_id, record=False)
        print(result.summary())
        return 0 if result.clean else 1
    finally:
        conn.close()


def cmd_hotspots(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    spots = hotspots.analyse(root, days=args.days, limit=args.limit)
    if args.json:
        print(json.dumps([s.to_dict() for s in spots], indent=2))
        return 0
    print(f"hotspot report for {root}  (last {args.days} days)\n")
    print(hotspots.format_table(spots, root))
    if spots:
        print("\nSuggested hot_paths for .ai/project.yaml:")
        print("hot_paths:")
        for spot in spots[: min(5, len(spots))]:
            print(f"  - {spot.path}")
    return 0


def cmd_task_add(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    paths = [p.strip() for p in (args.owns or "").split(",") if p.strip()]
    deps = [int(d) for d in (args.depends or "").replace(" ", "").split(",") if d]
    identifier = planning.create(root, title=args.title, description=args.description or "",
        kind=args.kind, role=args.role, expected_write=paths, depends_on=deps,
        gate_level=args.gate, job_id=getattr(args, "job", None),
        complexity=getattr(args, "complexity", "standard"), model_profile=getattr(args, "model_profile", None))
    print(f"saved task {identifier}: {args.title}")
    return 0


def cmd_job(args):
    root = _root_or_exit(args.path)
    if args.job_command == "create":
        request = Path(args.request_file).read_text(encoding="utf-8")
        job = jobs.create(root, args.job, request, args.coordinator, reviewers=args.reviewer)
        print(f"Created job {job['id']}; coordinator policy: {job['coordinator']} (model pinned on first launch)")
    elif args.job_command == "update":
        jobs.amend(root, args.job, Path(args.request_file).read_text(encoding="utf-8"), user=True)
        print("User correction saved; coordinator will replan before new work or integration.")
    elif args.job_command == "start":
        from .service import start
        print(start(root, args.max_workers))
    else:
        conn = db.connect(root)
        try:
            for row in conn.execute("SELECT * FROM jobs ORDER BY id"):
                print(json.dumps(dict(row), indent=2))
        finally:
            conn.close()
    return 0


def cmd_review(args):
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        reviews.approve(conn, load_project(root), args.task, args.head, args.verdict,
                        "operator", Path(args.evidence_file).read_text(encoding="utf-8"))
        print("Review recorded for " + args.head)
    finally:
        conn.close()
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    """Stop a task for good, releasing whatever it holds.

    The gap this fills: every other way out of the graph either needed an agent
    session (the MCP `task_status` tool) or put the task back in the queue
    (`discard`). A task created by mistake had nowhere to go.
    """
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        task = db.get_task(conn, args.task)
        if task is None:
            sys.stderr.write(f"Task {args.task} not found.\n")
            return 2
        current = str(task["status"])
        if current == sm.CANCELLED:
            print(f"task {args.task} is already CANCELLED")
            return 0
        if not sm.can(current, sm.CANCELLED, "human"):
            sys.stderr.write(
                f"Task {args.task} is {current} and cannot be cancelled from there.\n"
                f"Legal from {current}: "
                f"{', '.join(sm.TRANSITIONS.get(current, ())) or 'none'}\n"
            )
            return 1
        released = db.release_leases(conn, args.task, reason="cancelled by operator")
        db.set_status(conn, args.task, sm.CANCELLED, actor="human",
                      cause=args.reason or "cancelled by operator")
        print(f"task {args.task} cancelled ({current} -> CANCELLED), "
              f"{released} lease(s) released")
        if task.get("worktree"):
            print(f"  its worktree is left in place: {task['worktree']}")
            print(f"  remove it with `agentkit discard {args.task}` if you do not "
                  "want the work")
    finally:
        conn.close()
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    task_id = args.task or active_task_id()
    if task_id is None:
        sys.stderr.write("No task given and none active. Pass a task id.\n")
        return 2
    conn = db.connect(root)
    try:
        data = briefs.build(conn, load_project(root), int(task_id))
        if data is None:
            sys.stderr.write(f"Task {task_id} not found.\n")
            return 2
        print(briefs.render(data))
    finally:
        conn.close()
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    project = load_project(root)
    # Run where the caller is standing, not where the config lives. Inside a
    # worker's worktree `find_project_root` deliberately resolves back to the main
    # checkout — that is where tasks.db has to live — so gating on `root` would
    # silently test a different tree than the branch under review, and report a
    # pass for code the caller never ran. This matches the MCP `gate_run` tool,
    # which has always used the caller's directory.
    cwd = Path(args.path).resolve() if args.path else Path(os.getcwd()).resolve()
    result = gates.run_gate(project, args.level, cwd=cwd)
    if cwd != root:
        print(f"running in {cwd} (config from {root})")
    print(result.summary())
    return 0 if result.passed else 1


def cmd_lease_check(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    conn = db.connect(root)
    try:
        from .paths import resolve_within

        rel = resolve_within(args.file, root)
        if rel is None:
            print(f"{args.file} is outside {root}")
            return 2
        verdict = leases.decide(conn, load_project(root), rel, args.task or active_task_id())
        status = "ALLOW" if verdict.allowed else "BLOCK"
        print(f"{status}  {rel}\n  {verdict.reason}")
        return 0 if verdict.allowed else 1
    finally:
        conn.close()


def cmd_launch(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    project = load_project(root)
    from .locking import exclusive
    conn = db.connect(root)
    try:
        with exclusive(root, "scheduler"):
            task = db.get_task(conn, args.task)
            if task is None:
                raise ValueError(f"task {args.task} not found")
            if args.worktree:
                raise ValueError("worktrees are assigned by the supervisor; omit --worktree")
            from .capabilities import load_cache
            name = adapters.canonical_name(args.agent)
            capabilities = load_cache(root)
            chosen, reason = scheduler.choose_adapter(task, {name: capabilities[name]} if name in capabilities else {},
                unavailable=scheduler.unavailable_adapters(conn))
            if not chosen:
                raise ValueError(reason)
            from .worktrees import path_for
            plan = scheduler.LaunchPlan(task, chosen, path_for(root, task), task["generation"] + 1, reason)
            ok, detail = scheduler.launch(conn, root, project, plan, dry_run=args.dry_run)
            print(detail)
            return 0 if ok else 1
    finally:
        conn.close()


def cmd_setup_codex(args: argparse.Namespace) -> int:
    """Install the role profiles into CODEX_HOME, pointing at this orchestrator."""
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    if not codex_home.is_dir():
        sys.stderr.write(f"CODEX_HOME not found at {codex_home}. Is Codex installed?\n")
        return 2
    templates = Path(__file__).resolve().parents[2] / "plugins" / "agentkit" / "templates"
    profiles = sorted((templates / "codex-profiles").glob("*.config.toml"))
    if not profiles:
        sys.stderr.write(f"No profile templates found under {templates}.\n")
        return 2
    orchestrator = str(Path(__file__).resolve().parents[1])
    written = []
    for template in profiles:
        target = codex_home / template.name
        if target.exists() and not args.force:
            print(f"  = {target.name} (exists, left alone)")
            continue
        target.write_text(
            template.read_text(encoding="utf-8").replace(
                "REPLACE_WITH_ORCHESTRATOR_PATH", orchestrator.replace("\\", "/")
            ),
            encoding="utf-8",
        )
        written.append(target.name)
        print(f"  + {target.name}")
    print(f"\nInstalled {len(written)} profile(s) into {codex_home}")
    print("Use with:  codex exec -p agentkit-implementer --cd <worktree> -s workspace-write")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    root = find_project_root(args.path or os.getcwd()) or Path(os.getcwd())
    results = probe.probe_all(root, functional=args.functional)
    if not results:
        sys.stderr.write("No agent installations found.\n")
        return 2
    for result in results:
        print(result.summary())
        print()
    if not args.functional:
        print("Static probe only: flags and feature stages were checked, behaviour was not.")
        print("Run `agentkit probe --functional` to launch each agent and verify that its")
        print("write guards actually block. That costs tokens and is the only real proof.")
    print(f"\nCached to {root / '.ai' / 'capabilities.json'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("tooling")
    for tool in ("uv", "git"):
        found = shutil.which(tool)
        print(f"  [{'ok  ' if found else 'MISS'}] {tool:<12} {found or 'not found'}")
    for adapter, install in adapters.installed():
        print(f"  [ok  ] {adapter.name:<12} {install.version}  ({install.source})")
        print(f"         {install.path}")
    missing = [a.name for a in adapters.all_adapters() if a.detect() is None]
    for name in missing:
        print(f"  [MISS] {name:<12} not found")

    root = find_project_root(args.path or os.getcwd())
    print(f"\nproject: {root or 'not found'}")
    if root:
        project = load_project(root)
        print(f"  .ai/project.yaml : {'yes' if (root / '.ai' / 'project.yaml').is_file() else 'NO'}")
        print(f"  gates declared   : {', '.join(project.gates) or 'none'}")
        setup = project.worktree_setup
        print(f"  worktree_setup   : {len(setup)} command(s)")
        print(f"  hot_paths        : {len(project.hot_paths)}")
        print(f"  AGENTS.md        : {'yes' if (root / 'AGENTS.md').is_file() else 'NO'}")

        # The failure this catches is otherwise invisible until a worker hits it,
        # and by then it has burned an attempt.
        broken = gates.unrunnable_in_worktree(project)
        if broken:
            print(f"\n  [WARN] {len(broken)} gate command(s) cannot run in a worker's worktree:")
            for level, command, reason in broken:
                print(f"         {level}: {command}")
                print(f"           {reason}")
            print("         A worktree never inherits an installed environment, so this")
            print("         gate fails for every worker and burns an attempt each time.")
            print("         Fix: add `worktree_setup:` to .ai/project.yaml to build one.")

        conn = db.connect(root)
        try:
            print(f"  tasks            : {len(db.list_tasks(conn))}")
            print(f"  active leases    : {len(db.active_leases(conn))}")
        finally:
            conn.close()

        cached = load_capability_cache(root)
        print("\ncapabilities (cached probe)")
        if not cached:
            print("  none — run `agentkit probe`")
        for name, caps in sorted(cached.items()):
            derived = caps.derived()
            summary = ", ".join(k for k, v in sorted(derived.items()) if v) or "none"
            stale = " STALE" if _probe_stale(name, caps) else ""
            print(f"  {name:<12} {caps.version}{stale}")
            print(f"               qualifies for: {summary}")
            for kind in ("HOTSPOT", "SAFE_PARALLEL", "TEST_ONLY", "RESEARCH"):
                missing = caps.missing_for(kind)
                verdict = "yes" if not missing else f"no ({', '.join(missing)})"
                print(f"               {kind:<14} {verdict}")
            if not caps.has("write_worker_safe"):
                print("               -> cannot run unattended write tasks: "
                      + caps.notes.get("workspace_sandbox", "no proven confinement"))
                if name == "claude-code":
                    print('               -> to enable: add {"sandbox": {"enabled": true}} '
                          "to ~/.claude/settings.json, then re-run `agentkit probe`")
    return 0


def _probe_stale(adapter_name: str, caps: Any) -> bool:
    adapter = adapters.get(adapter_name)
    install = adapter.detect() if adapter else None
    return bool(install and install.version != caps.version)


def cmd_models(args: argparse.Namespace) -> int:
    root = _root_or_exit(args.path)
    project = load_project(root)
    conn = db.connect(root)
    try:
        entries = models.catalog(project)
        for entry in entries:
            entry["eligible_now"] = models.usable(conn, project, models.profile(project, entry["name"]))
        if args.json:
            print(json.dumps(entries, indent=2))
        else:
            print(f"Model catalog verified: {models.VERIFIED_AT}; approved upgrades apply to new sessions.")
            print("Coordinator/reviewer: Astra -> Opus, xhigh; coordinator stays pinned after starting.")
            print("Worker preference: Sol -> Opus -> Sonnet (easy only); Qwen by explicit easy research assignment.")
            for entry in entries:
                state = "eligible" if entry["eligible_now"] else "disabled/unavailable"
                print(f"  {entry['name']}: {entry['model']} [{entry['provider']}, {state}]")
            print("Eligibility reflects saved account/model state, not a live inference test. Use providers check and probe.")
    finally:
        conn.close()
    return 0


_WORKERS_HELP = ("0 = every eligible independent task (default via max_workers in .ai/project.yaml, "
                 "which defaults to 0); a positive number is an optional cap")


def cmd_live(args: argparse.Namespace) -> int:
    from .live import run
    return run(_root_or_exit(args.path), task_id=args.task, follow=args.follow,
               poll_seconds=args.poll, json_output=args.json)


def cmd_recovery(args: argparse.Namespace) -> int:
    from .cli_recovery import run
    return run(args, _root_or_exit(args.path))


def cmd_manager(args: argparse.Namespace) -> int:
    from .cli_manager import run
    return run(args, _root_or_exit(args.path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentkit", description=__doc__)
    from .version import report
    parser.add_argument("--version", action="version", version=report())
    parser.add_argument("--path", help="project directory (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    from .cli_manager import configure
    configure(sub, cmd_manager)
    from .cli_recovery import configure as configure_recovery
    configure_recovery(sub, cmd_recovery)
    from .cli_workflow import configure as configure_workflow
    configure_workflow(sub, _root_or_exit)
    live_parser = sub.add_parser("live", help="read-only live worker, reviewer and manager dashboard")
    live_parser.add_argument("--task", type=int)
    live_parser.add_argument("--follow", action="store_true")
    live_parser.add_argument("--poll", type=float, default=2.0)
    live_parser.add_argument("--json", action="store_true")
    live_parser.set_defaults(func=cmd_live)
    p = sub.add_parser("models", help="show ranked model policy and saved availability")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_models)
    job = sub.add_parser("job", help="durable user requests and coordinator jobs")
    js = job.add_subparsers(dest="job_command", required=True)
    p = js.add_parser("create")
    p.add_argument("job")
    p.add_argument("--request-file", required=True)
    p.add_argument("--coordinator", choices=["auto", "codex", "claude-code", "claude"], default="auto",
                   help="auto selects Astra then Opus xhigh before pinning the job")
    p.add_argument("--reviewer", action="append", choices=adapters.selectable_names())
    p.set_defaults(func=cmd_job)
    p = js.add_parser("update")
    p.add_argument("job")
    p.add_argument("--request-file", required=True)
    p.set_defaults(func=cmd_job)
    p = js.add_parser("status")
    p.set_defaults(func=cmd_job)
    p = js.add_parser("start")
    p.add_argument("--max-workers", type=concurrency.argument, default=None, help=_WORKERS_HELP)
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("review", help="record an operator's commit-specific review")
    p.add_argument("task", type=int)
    p.add_argument("--head", required=True)
    p.add_argument("--verdict", required=True, choices=("PASS", "CHANGES", "REJECT"))
    p.add_argument("--evidence-file", required=True)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("init", help="onboard a repository")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="task graph, leases and open amendments")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hotspots", help="rank files that force agents to work one at a time")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hotspots)

    p = sub.add_parser("brief", help="print a task brief")
    p.add_argument("task", nargs="?", type=int)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("gate", help="run a declared gate")
    p.add_argument("level", nargs="?", default="fast")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("check", help="would a task be allowed to edit this file?")
    p.add_argument("file")
    p.add_argument("--task", type=int)
    p.set_defaults(func=cmd_lease_check)

    p = sub.add_parser("launch", help="start a worker for a task")
    p.add_argument("task", type=int)
    p.add_argument("--agent", choices=adapters.selectable_names(), default="claude-code")
    p.add_argument("--worktree")
    p.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("doctor", help="check tooling, project wiring and cached capabilities")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("probe", help="measure what each installed agent can actually do")
    p.add_argument("--functional", action="store_true",
                   help="launch each agent to verify its write guards really block "
                        "(costs tokens; the only real proof)")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("reconcile", help="bring runtime state back in line with the spec and git")
    p.add_argument("--adopt", action="store_true",
                   help="keep tasks whose worker process is still alive")
    p.add_argument("--rebuild", action="store_true",
                   help="rebuild runtime state from git after losing the database")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("run", help="reconcile, then launch eligible workers")
    p.add_argument("--max-workers", type=concurrency.argument, default=None, help=_WORKERS_HELP)
    p.add_argument("--dry-run", action="store_true",
                   help="show launch commands without starting agents; reconcile runtime bookkeeping")
    p.add_argument("--watch", action="store_true",
                   help="keep scheduling until the queue is idle; survives provider "
                        "usage limits and resumes automatically when they reset")
    p.add_argument("--poll", type=int, default=watch.DEFAULT_POLL_SECONDS,
                   help="seconds between passes when nothing is waiting on a clock")
    p.set_defaults(func=cmd_run)

    pr = sub.add_parser("providers", help="provider/account availability and cooldowns")
    pr_sub = pr.add_subparsers(dest="provider_command", required=False)
    pr.set_defaults(func=cmd_providers, provider_command=None,
                    provider=None, account="default", reason=None)
    q = pr_sub.add_parser("status", help="show availability")
    q.set_defaults(func=cmd_providers, provider=None, account="default", reason=None)
    q = pr_sub.add_parser("clear", help="mark a provider available again now")
    q.add_argument("provider")
    q.add_argument("--account", default="default")
    q.set_defaults(func=cmd_providers, reason=None)
    q = pr_sub.add_parser("cooldown", help="manually mark a provider unavailable")
    q.add_argument("provider")
    q.add_argument("--account", default="default")
    q.add_argument("--reason")
    q.set_defaults(func=cmd_providers)
    q = pr_sub.add_parser("check", help="refresh account availability (Claude may use a small tool-free request)")
    q.add_argument("provider", nargs="?", choices=adapters.selectable_names())
    q.set_defaults(func=cmd_providers)

    p = sub.add_parser("integrate", help="merge INTEGRATION_READY tasks behind the full gate")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--skip-gate", action="store_true",
                   help="deprecated: integration refuses requests to skip its full gate")
    p.set_defaults(func=cmd_integrate)

    op = sub.add_parser("operator", help="hold leases for manual edits (the human escape hatch)")
    op_sub = op.add_subparsers(dest="operator_command", required=True)
    p = op_sub.add_parser("acquire", help="claim paths so you can edit them by hand")
    p.add_argument("paths", nargs="+")
    p.add_argument("--reason", help="recorded in the event log")
    p.set_defaults(func=cmd_operator)
    p = op_sub.add_parser("release", help="give the paths back")
    p.set_defaults(func=cmd_operator)
    p = op_sub.add_parser("status", help="what you currently hold")
    p.set_defaults(func=cmd_operator)

    p = sub.add_parser("scan-secrets", help="find credentials reachable from a worktree")
    p.add_argument("--worktree")
    p.set_defaults(func=cmd_scan_secrets)

    p = sub.add_parser("audit", help="run the L5 worktree audit by hand")
    p.add_argument("--task", type=int)
    p.add_argument("--worktree")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("adopt", help="re-attach a STALE task to a fresh worker")
    p.add_argument("task", type=int)
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("discard", help="throw away a STALE task's worktree and reset it")
    p.add_argument("task", type=int)
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("why", help="explain a scheduling or merge decision")
    p.add_argument("question", choices=("launched", "stopped", "serialized", "merge-failed"))
    p.add_argument("task", type=int)
    p.set_defaults(func=cmd_why)

    p = sub.add_parser("events", help="the event log")
    p.add_argument("--task", type=int)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("setup-codex", help="install AgentKit role profiles into CODEX_HOME")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup_codex)

    task = sub.add_parser("task", help="task operations")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    p = task_sub.add_parser("add", help="add a task")
    p.add_argument("title")
    p.add_argument("--description", default="")
    p.add_argument("--kind", default="SAFE_PARALLEL", choices=db.TASK_KINDS)
    p.add_argument("--role", default="implementer")
    p.add_argument("--owns", help="comma-separated globs this task may edit")
    p.add_argument("--depends", help="comma-separated task ids")
    p.add_argument("--gate", default="fast")
    p.add_argument("--job")
    p.add_argument("--complexity", choices=["easy", "standard", "complex"], default="standard")
    p.add_argument("--model-profile", choices=["sol", "opus", "sonnet", "qwen"])
    p.set_defaults(func=cmd_task_add)
    p = task_sub.add_parser("cancel", help="stop a task for good and release its leases")
    p.add_argument("task", type=int)
    p.add_argument("--reason", help="recorded in the event log")
    p.set_defaults(func=cmd_task_cancel)

    from .cli_workspaces import configure as configure_workspaces
    configure_workspaces(sub)
    from .project_usage import configure as configure_usage
    configure_usage(sub)
    from .terminal_manager import configure as configure_terminal
    configure_terminal(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ValueError, PermissionError, TimeoutError) as exc:
        sys.stderr.write(f"agentkit: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
