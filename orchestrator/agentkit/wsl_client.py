"""Linux-only hook/MCP socket proxy. This module never imports database code."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from contextlib import suppress
from secrets import token_hex

from .wsl_mcp_protocol import CLOSE, POLL, SEND, envelope
from .wsl_transport import SOCKET_ENV, TIMEOUTS, recv_message, send_message


def request(body: dict) -> dict:
    path = os.environ.get(SOCKET_ENV)
    if not path:
        raise RuntimeError("Worker socket is unavailable")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:  # type: ignore[attr-defined]  # Linux-only runtime.
        peer.settimeout(TIMEOUTS.get(str(body.get("op")), 30) + 5)
        peer.connect(path)
        send_message(peer, body)
        response = recv_message(peer)
    if response.get("ok") is not True or not isinstance(response.get("body"), dict):
        raise RuntimeError(response.get("error") or "Host response is invalid")
    result = response["body"]
    if result.get("op") != body.get("op") or result.get("error"):
        raise RuntimeError(result.get("error") or "Host response operation differs")
    return result


def proxy(stream_in, stream_out) -> None:
    """Read client RPCs independently of subscription events and tool responses."""
    session, stopped, errors = token_hex(16), threading.Event(), []
    def exchange(method, message=None):
        return request({"op": "mcp", "session": session, "message": envelope(method, message)})
    def forward():
        try:
            for line in stream_in:
                exchange(SEND, json.loads(line))
        except Exception as exc:
            errors.append(exc)
        finally:
            stopped.set()
    threading.Thread(target=forward, daemon=True).start()
    try:
        while not stopped.is_set():
            reply = exchange(POLL)
            for message in reply.get("messages", []):
                stream_out.write(json.dumps(message) + "\n")
                stream_out.flush()
        if errors:
            raise errors[0]
    finally:
        with suppress(Exception):
            exchange(CLOSE)


def main() -> int:
    try:
        mode = sys.argv[1]
        if mode == "hook":
            reply = request({"op": "hook", "event": sys.argv[2], "payload": json.load(sys.stdin)})
            sys.stdout.write(reply.get("stdout", ""))
            sys.stderr.write(reply.get("stderr", ""))
            return int(reply.get("exit", 2))
        if mode != "mcp":
            raise ValueError("Unknown proxy mode")
        proxy(sys.stdin, sys.stdout)
        return 0
    except Exception as exc:
        sys.stderr.write(f"[AgentKit] Blocked: WSL host authority unavailable: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
