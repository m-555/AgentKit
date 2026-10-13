"""Narrow opt-in local operator controls, separate from the read-only snapshot."""
from __future__ import annotations

import hmac
import json
import secrets

from . import db, user_acceptance, workflow_setup

MAX_BODY = 16384
#: A job with its screenshots: ten 8 MB images, base64 encoded, and the words.
JOB_ROUTES = ("/api/job", "/api/job-update")
MAX_JOB_BODY = 115_000_000


def body_limit(route: str) -> int:
    return MAX_JOB_BODY if route in JOB_ROUTES else MAX_BODY


def _job(root, route, request):
    """Save a job, or an update to one, in the user's words with their screenshots."""
    from . import job_input
    from .repo_settings import operator_only
    from .service_control import status
    operator_only()
    if route == "/api/job":
        if "text" not in request or set(request) - {"text", "acceptance", "images"}:
            raise ValueError("provide text, and optionally acceptance and images")
        job = job_input.create(root, request["text"], acceptance=request.get("acceptance"), images=request.get("images"))
    else:
        if set(request) - {"job_id", "text", "images"} or not {"job_id", "text"} <= set(request):
            raise ValueError("provide job_id and text, and optionally images")
        job = job_input.update(root, request["job_id"], request["text"], images=request.get("images"))
    running = status(root)["state"] != "stopped"
    shots = len(job["requests"][-1].get("attachments") or [])
    return {"job": job["id"], "message": f"Job {job['id']} saved" + (f" with {shots} screenshot(s)" if shots else "")
            + (". The assigner plans it next." if running else ". Press Start to let the assigner plan it.")}


def _repository(root, route, request):
    """Settings, Start/Stop and provider switches for the repository the page shows."""
    from . import repo_controls, repo_settings
    shapes = {"/api/settings": {"settings"}, "/api/repo-start": set(), "/api/repo-stop": set(),
              "/api/provider": {"provider", "action", "reason"}, "/api/local-thinking": {"mode"},
              "/api/task-retry": {"task"}, "/api/problem": {"problem", "action", "text"}}
    if set(request) != shapes[route]:
        raise ValueError("provide " + (", ".join(sorted(shapes[route])) or "an empty object") + " only")
    if route == "/api/settings":
        return repo_settings.apply(root, request["settings"])
    if route == "/api/repo-start":
        return repo_controls.start(root)
    if route == "/api/repo-stop":
        return repo_controls.stop(root)
    if route == "/api/task-retry":
        return repo_controls.retry(root, request["task"])
    if route == "/api/problem":
        return repo_controls.answer(root, request["problem"], request["action"], request["text"])
    if route == "/api/provider":
        return repo_controls.provider(root, request["provider"], request["action"], request["reason"])
    return repo_controls.set_thinking(request["mode"])


class Controls:
    def __init__(self, root, enabled=False):
        self.root = root
        self.enabled = enabled
        self.token = secrets.token_urlsafe(32) if enabled else None
        if enabled:
            user_acceptance._operator_only()

    def snapshot(self):
        try:
            pending = db.redact(user_acceptance.queue(self.root))
        except Exception:
            return {"enabled": self.enabled, "token": self.token, "jobs": [],
                    "message": "Review state unavailable; no decisions can be submitted."}
        return {"enabled": self.enabled, "token": self.token, "jobs": pending}

    def post(self, route, headers, stream):
        if not self.enabled:
            return 405, {"message": "Operator controls are disabled."}
        token = headers.get("X-AgentKit-Token", "")
        if not hmac.compare_digest(token, self.token or ""):
            return 403, {"message": "Current local operator token required."}
        if headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            return 415, {"message": "Use application/json."}
        if headers.get("Transfer-Encoding"):
            return 400, {"message": "Chunked requests are unsupported."}
        try:
            length = int(headers.get("Content-Length", "0"))
            if not 0 < length <= body_limit(route):
                return 413, {"message": f"Request must be 1-{body_limit(route)} bytes."}
            request = json.loads(stream.read(length))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if route == "/api/review-decision":
                required = {"job_id", "revision", "head", "digest", "verdict", "evidence"}
                if set(request) != required:
                    raise ValueError("provide exact job, revision, head, digest, verdict and evidence")
                return 200, user_acceptance.decide(self.root, **request)
            if route == "/api/team-profile":
                return 200, workflow_setup.apply(self.root, request)
            if route == "/api/manager-chat":
                from .manager_chat import post
                if set(request) != {"job_id", "text"}:
                    raise ValueError("provide job_id and text only")
                post(self.root, request["job_id"], request["text"])
                return 200, {"message": "Sent. The manager receives it on its next turn; a sleeping coordinator wakes."}
            if route in ("/api/settings", "/api/repo-start", "/api/repo-stop", "/api/provider",
                         "/api/local-thinking", "/api/task-retry", "/api/problem"):
                return 200, _repository(self.root, route, request)
            if route in JOB_ROUTES:
                return 200, _job(self.root, route, request)
            if route in ("/api/phone", "/api/phone-test"):
                from . import phone_view
                if route == "/api/phone-test" and request:
                    raise ValueError("provide an empty object")
                return 200, phone_view.save(request) if route == "/api/phone" else phone_view.test()
            if route == "/api/terminal/input":
                from .terminal_manager import send
                if set(request) != {"job_id", "text"}:
                    raise ValueError("provide manager job_id and text only")
                send(self.root, request["job_id"], request["text"])
                return 200, {"message": "Input delivered to the registered CLI manager; agent completion is not implied."}
            return 404, {"message": "Unknown operator action."}
        except PermissionError as exc:
            return 403, {"message": str(exc)}
        except (ValueError, OSError, TimeoutError) as exc:
            return 409, {"message": str(exc)}
