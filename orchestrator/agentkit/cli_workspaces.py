"""Operator lifecycle controls; preparation and migration make no model calls."""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .config import load_project
from .paths import find_project_root


def command(args):
    root = find_project_root(args.path or Path.cwd())
    if root is None:
        raise ValueError("No AgentKit project found")
    project = load_project(root)
    conn = db.connect(root)
    try:
        action = args.workspace_action
        if action == "recover":
            from .workspace_migration import recover
            result = recover(conn, project)
        elif action == "wake-cancel":
            from .native_wake_control import cancel
            result = cancel(root, args.thread, args.reason)
        else:
            task = db.get_task(conn, args.task)
            if not task or not task.get("worktree"):
                raise ValueError("Task has no recorded workspace")
            if action == "move":
                from .workspace_migration import move
                result = move(conn, project, task, args.to, preserve_dirty=args.preserve_dirty)
            elif action == "archive":
                from .workspace_archive import archive
                result = archive(conn, project, task)
            elif action == "setup-repair":
                from .environment_prepare import repair
                repair(project, task["worktree"])
                result = {"task": args.task, "setup": "repair_requested"}
            else:
                from .environment_prepare import prepare
                result = prepare(project, task["worktree"], task).to_dict()
        print(json.dumps(result, indent=2))
        return 1 if isinstance(result, dict) and result.get("passed") is False else 0
    finally:
        conn.close()


def configure(sub):
    group = sub.add_parser("workspace", help="worktree migration, setup and retirement")
    actions = group.add_subparsers(dest="workspace_action", required=True)
    for name in ("move", "recover", "archive", "setup-repair", "setup-prepare", "wake-cancel"):
        parser = actions.add_parser(name)
        parser.set_defaults(func=command)
        if name == "wake-cancel":
            parser.add_argument("--thread", required=True)
            parser.add_argument("--reason", required=True)
        elif name != "recover":
            parser.add_argument("--task", type=int, required=True)
        if name == "move":
            parser.add_argument("--to", type=Path, required=True)
            parser.add_argument("--preserve-dirty", action="store_true",
                                help="Explicitly preserve and verify all uncommitted source")
