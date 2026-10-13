"""Start, stop and provider switches the view offers for one repository.

Start is the one action that lets a repository's agents work: it lifts the pause and
starts the background service, whose supervisor launches the pinned manager and the
workers. Stop ends the service; worker sessions already running finish on their own
and nothing new launches. Local model thinking belongs to the local router, so it is
machine-wide and lasts until the router restarts.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from . import db, project_registry, providers
from .config import load_project
from .locking import atomic_write, exclusive
from .repo_settings import PROVIDERS, operator_only

THINKING = {"default": "default", "on": True, "off": False}


def _set_paused(root: Path, paused: bool) -> None:
    with exclusive(root, "supervisor"), exclusive(root, "scheduler"):
        raw = dict(load_project(root).raw)
        if (raw.get("execution_paused") is True) != paused:
            raw["execution_paused"] = paused
            atomic_write(root / ".ai" / "project.yaml", yaml.safe_dump(raw, sort_keys=False))


def start(root) -> dict:
    operator_only()
    root = Path(root)
    from . import service
    from .service_control import status
    _set_paused(root, False)
    project_registry.register(root)
    message = service.start(root)
    conn = db.connect(root)
    try:
        db.log_event(conn, None, "repository_started", cause="operator pressed Start in the AgentKit view")
    finally:
        conn.close()
    return {"message": message, "service": status(root)}


def stop(root) -> dict:
    operator_only()
    from .service_control import stop as stop_service
    result = stop_service(Path(root))
    result["message"] = ("Stopped. Sessions already running finish on their own; nothing new launches."
                         if result["stopped"] else result["note"] or "Already stopped.")
    return result


def retry(root, task_id) -> dict:
    """Resume a task stopped on an infrastructure problem, after the operator fixed it."""
    operator_only()
    if type(task_id) is not int:
        raise ValueError("choose a task to retry")
    from . import infra_stop
    conn = db.connect(Path(root))
    try:
        return {"message": infra_stop.retry(conn, load_project(root), task_id)}
    finally:
        conn.close()


def answer(root, problem, action, text) -> dict:
    """Reply to a problem from the view: retry, accept, skip, dismiss, hint or plan."""
    operator_only()
    if type(problem) is not int or not isinstance(action, str) or not isinstance(text, str):
        raise ValueError("choose a problem and a reply")
    from .problems import act
    return {"message": act(root, problem, action, text, by="view")}


def provider(root, name: str, action: str, reason: str = "") -> dict:
    operator_only()
    if name not in PROVIDERS or action not in ("hold", "clear"):
        raise ValueError(f"choose hold or clear for one of {', '.join(PROVIDERS)}")
    conn = db.connect(Path(root))
    try:
        if action == "hold":
            if not reason.strip() or len(reason) > 500:
                raise ValueError("say why the provider is held (1-500 characters)")
            state = providers.hold(conn, name, reason=reason)
        else:
            state = providers.clear_cooldown(conn, name, reason="operator cleared it in the AgentKit view")
    finally:
        conn.close()
    return state.to_dict()


def _router() -> str:
    from .adapters.local_opencode import local_config
    endpoint = urlsplit(local_config()["options"]["baseURL"])
    return f"http://{endpoint.hostname}:{endpoint.port or 80}"


def _call(path: str, body: dict | None = None, timeout: float = 5) -> dict:
    opener = build_opener(ProxyHandler({}))  # loopback only, never through a proxy
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(_router() + path, data=data, method="POST" if data else "GET",
                      headers={"Content-Type": "application/json"} if data else {})
    with opener.open(request, timeout=timeout) as reply:
        value = json.loads(reply.read().decode("utf-8"))
    return value if isinstance(value, dict) else {}


def thinking() -> dict:
    """The local router's thinking switch: default (per model), on or off for every model."""
    try:
        health = _call("/health", timeout=0.5)  # a stopped router must not slow the settings page
    except (OSError, ValueError) as exc:
        return {"available": False, "mode": None, "message": f"Local router not reachable: {exc}"}
    override = health.get("thinkingOverride")
    return {"available": True, "mode": "default" if override is None else "on" if override else "off"}


def set_thinking(mode: str) -> dict:
    operator_only()
    if mode not in THINKING:
        raise ValueError("thinking is default, on or off")
    _call("/thinking", {"enabled": THINKING[mode]})
    return thinking()
