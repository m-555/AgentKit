"""`agentkit task retry` and `agentkit problem`: answer what stopped, from the terminal."""
from __future__ import annotations


def run(args, root) -> int:
    from . import db, infra_stop
    from .config import load_project
    from .repo_settings import operator_only
    operator_only()
    conn = db.connect(root)
    try:
        print(infra_stop.retry(conn, load_project(root), args.task))
    finally:
        conn.close()
    return 0


def problems_list(args, root) -> int:
    from . import db, problems
    conn = db.connect(root)
    try:
        rows = problems.listing(conn)
    finally:
        conn.close()
    if not rows:
        print("No problems wait for you.")
    for row in rows:
        waiting = " (with the assigner)" if row["status"] == "WAITING" else ""
        print(f"{row['code']}  {row['title']}{waiting}\n    {row['detail']}\n"
              f"    replies: {', '.join(row['options']) or 'none; change it in the view'}")
    return 0


def problems_act(args, root) -> int:
    from . import problems
    from .repo_settings import operator_only
    operator_only()
    print(problems.act(root, args.code, args.action, " ".join(args.text), by="cli"))
    return 0


def configure(task_sub, root_or_exit) -> None:
    p = task_sub.add_parser("retry", help="resume a task stopped on a tool, permission or setup problem")
    p.add_argument("task", type=int)
    p.set_defaults(func=lambda args: run(args, root_or_exit(args.path)))


def configure_problems(sub, root_or_exit) -> None:
    problem = sub.add_parser("problem", help="what stopped and waits for you, and your replies")
    actions = problem.add_subparsers(dest="problem_command", required=True)
    p = actions.add_parser("list", help="problems that wait for you")
    p.set_defaults(func=lambda args: problems_list(args, root_or_exit(args.path)))
    p = actions.add_parser("act", help="reply to a problem: retry, accept, skip, dismiss, hint or plan")
    p.add_argument("code", help="the problem's code, or its number")
    p.add_argument("action", choices=["retry", "accept", "skip", "dismiss", "hint", "plan", "answer"])
    p.add_argument("text", nargs="*", help="your words, for hint or plan")
    p.set_defaults(func=lambda args: problems_act(args, root_or_exit(args.path)))
