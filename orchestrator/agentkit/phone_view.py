"""The view's phone settings: one mailbox for every repository, and a test message."""
from __future__ import annotations

from pathlib import Path

from . import phone_mail


def read(root) -> dict:
    from .config import load_project
    from .phone import state
    try:
        saved = phone_mail.password() is not None
    except OSError:
        saved = False
    try:
        delivery = state(load_project(Path(root)))
    except (OSError, ValueError):
        delivery = {}
    return {"settings": phone_mail.settings(), "password_saved": saved,
            "error": delivery.get("error"), "polled_at": delivery.get("polled_at")}


def save(request) -> dict:
    from .repo_settings import operator_only
    operator_only()
    if not isinstance(request, dict) or set(request) != {"settings", "password"} or not isinstance(request["password"], str):
        raise ValueError("provide settings and password (empty keeps the saved one)")
    phone_mail.save(request["settings"], password=request["password"].strip() or None)
    return {"message": "Phone settings saved for every repository."}


def test() -> dict:
    from .repo_settings import operator_only
    operator_only()
    config = phone_mail.settings()
    secret = phone_mail.password()
    if not (config["agent_address"] and config["user_address"] and secret):
        raise ValueError("save the AgentKit mailbox, its app password and your address first")
    phone_mail.send(config, secret, phone_mail.compose(
        config, "[AgentKit] Test message",
        "AgentKit can reach your phone. When a task stops, you get a message like this with the replies that "
        "fix it; answer with one word."))
    return {"message": f"Test message sent to {config['user_address']}."}
