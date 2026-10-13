"""One view for every AgentKit repository: which one is shown, and the list to choose from.

The page names its repository in the `agentkit_repo` cookie. Only an id from the
repository list selects one; anything else falls back to the view's own repository,
so a request can never point the view at an arbitrary path.
"""
from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from . import project_registry

COOKIE = "agentkit_repo"


def selected(default: Path, cookie_header: str | None, *, home: bool) -> Path:
    if not home or not cookie_header:
        return default
    try:
        jar = SimpleCookie(cookie_header)
    except CookieError:
        return default
    morsel = jar.get(COOKIE)
    chosen = project_registry.resolve(morsel.value) if morsel and morsel.value.isalnum() else None
    return chosen or default


def repos(current: Path) -> dict:
    from .config import load_project
    from .jobs_view import open_count
    from .service_control import status
    rows = []
    for row in project_registry.projects():
        try:
            paused = load_project(row["root"]).raw.get("execution_paused") is True
        except (OSError, ValueError):
            paused = None
        rows.append({**row, "state": status(row["root"])["state"], "execution_paused": paused,
                     "problems": open_count(row["root"]),
                     "selected": row["id"] == project_registry.ident(current)})
    if not any(row["selected"] for row in rows):
        rows.insert(0, {"id": project_registry.ident(current), "name": current.name, "root": str(current),
                        "last_used_at": None, "state": status(current)["state"], "execution_paused": None,
                        "selected": True, "unlisted": True})
    return {"repos": rows}


def phone(current: Path) -> dict:
    from .phone_view import read
    return read(current)


def jobs(current: Path) -> dict:
    from .jobs_view import read
    return read(current)


def settings(current: Path) -> dict:
    from .repo_controls import thinking
    from .repo_settings import read
    return {**read(current), "local_thinking": thinking()}
