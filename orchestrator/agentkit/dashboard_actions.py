"""Narrow opt-in local operator controls, separate from the read-only snapshot."""
from __future__ import annotations

import hmac
import json
import secrets

from . import db, user_acceptance, workflow_setup

MAX_BODY = 16384


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
            if not 0 < length <= MAX_BODY:
                return 413, {"message": "Request must be 1-16384 bytes."}
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
