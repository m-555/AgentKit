"""Small CLI transports for account metadata and bounded availability checks."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from ..secrets import worker_environment


def rpc(binary: str, method: str, timeout: float = 20, *, params=None, flags=None, cwd=None) -> dict:
    proc = subprocess.Popen([binary, *(flags or []), "app-server"], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace",
                            env=worker_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages: queue.Queue = queue.Queue()
    def read():
        assert proc.stdout
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout
    def send(payload):
        assert proc.stdin
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
    def receive(identifier):
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError("app-server exited before replying")
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
        raise TimeoutError("app-server request timed out")
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentkit", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": method, "params": params or {}})
        return receive(2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdin:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()


def codex_windows(payload: dict) -> list[dict]:
    buckets = payload.get("rateLimitsByLimitId") or {"codex": payload.get("rateLimits", {})}
    result = []
    for bucket, limits in buckets.items():
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if isinstance(window, dict) and window.get("usedPercent") is not None:
                duration = window.get("windowDurationMins")
                result.append({"bucket": bucket, "window": str(duration or name),
                               "used_percent": window["usedPercent"], "resets_at": window.get("resetsAt")})
    return result


def claude_windows(payload: dict) -> list[dict]:
    result = []
    for name, value in (payload.get("rate_limits") or {}).items():
        if isinstance(value, dict) and value.get("used_percentage") is not None:
            result.append({"bucket": "claude", "window": name,
                           "used_percent": value["used_percentage"], "resets_at": value.get("resets_at")})
    info = payload.get("rate_limit_info") or payload.get("rateLimitInfo") or {}
    if isinstance(info, dict) and (info.get("utilization") is not None or info.get("status") == "rejected"):
        result.append({"bucket": "claude", "window": info.get("rateLimitType", "unknown"),
                       "used_percent": 100 if info.get("status") == "rejected" else info["utilization"] * 100,
                       "resets_at": info.get("resetsAt")})
    return result
