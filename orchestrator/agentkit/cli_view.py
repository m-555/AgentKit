"""`agentkit view`: open the view with one command, and keep it online.

Starting the view meant a long interpreter path, the right flags and a terminal
kept open, and a view started without operator controls could not send. This
records that the view is wanted (the supervisor then restarts it whenever it is
down, see view_keeper), replaces a read-only view of this project, starts one
with operator controls outside the editor's process tree, and opens the browser.
`--stop` clears the wish and stops it.
"""
from __future__ import annotations

import webbrowser
from pathlib import Path


def stop(root) -> str:
    from . import view_keeper
    wish = view_keeper.state(root)
    view_keeper.save(root, {**wish, "always_on": False})
    port = int(wish.get("port") or 8766)
    current = view_keeper.probe(port)
    if current and (current.get("home") or current.get("root") == str(Path(root).resolve())) \
            and isinstance(current.get("pid"), int):
        view_keeper.end(current["pid"])
        return f"View stopped (pid {current['pid']}); the supervisor will not restart it."
    return "No view of this project is running; the supervisor will not start one."


def run(args, root, *, opener=webbrowser.open, ensure=None) -> int:
    from . import view_keeper
    if args.stop:
        print(stop(root))
        return 0
    from .project_registry import register
    register(root)
    view_keeper.save(root, {**view_keeper.state(root), "always_on": True, "port": args.port})
    result = (ensure or view_keeper.ensure)(root, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    if result == "port busy":
        print(f"Port {args.port} serves another project's view; choose another with --port.")
        return 1
    print(("The view is running" if result == "running" else "View started") + f" at {url} for every AgentKit "
          "repository, with sending enabled. Choose a repository, adjust Settings and press Start. "
          "It stays online: the AgentKit service restarts it whenever it stops. Stop it with `agentkit view --stop`.")
    if not args.no_open:
        opener(url)
    return 0


def configure(sub, root_or_exit) -> None:
    parser = sub.add_parser("view", help="open the AgentKit view and keep it online (sending enabled)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    parser.add_argument("--stop", action="store_true", help="stop the view and stop keeping it online")
    parser.set_defaults(func=lambda args: run(args, _root(args.path, root_or_exit)))


def _root(explicit, root_or_exit):
    """This repository, or outside one (after a restart) the most recently used AgentKit repository."""
    import os

    from .paths import find_project_root
    from .project_registry import is_project, projects
    found = find_project_root(explicit or os.getcwd())
    if found is not None and is_project(found):
        return found
    known = projects()
    return Path(known[0]["root"]) if known and not explicit else root_or_exit(explicit)
