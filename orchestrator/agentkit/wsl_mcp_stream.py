"""Bounded Windows MCP stdio duplex; long subscriptions never serialize RPCs."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading

from .secrets import worker_environment
from .wsl_transport import MAX_FRAME


class StreamChannel:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = subprocess.Popen([sys.executable, "-I", "-m", "agentkit.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=worker_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages: queue.Queue = queue.Queue(maxsize=32)
        self.failure = ""
        self.finished = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        assert self.proc.stdout
        try:
            while line := self.proc.stdout.readline(MAX_FRAME + 1):
                if len(line.encode("utf-8")) > MAX_FRAME or not line.endswith("\n"):
                    raise ValueError("MCP output exceeds the frame bound")
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid MCP output")
                self.messages.put_nowait(message)
        except (ValueError, queue.Full, OSError) as exc:
            self.failure = "MCP output backlog exceeded" if isinstance(exc, queue.Full) else str(exc)
        finally:
            self.finished.set()

    def send(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("Invalid proxied JSON-RPC message")
        if self.finished.is_set():
            raise RuntimeError(self.failure or "Host MCP exited")
        with self.lock:
            assert self.proc.stdin
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def poll(self):
        if self.failure:
            raise RuntimeError(self.failure)
        try:
            return [self.messages.get(timeout=1)]
        except queue.Empty:
            if self.finished.is_set():
                raise RuntimeError("Host MCP exited") from None
            return []

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
