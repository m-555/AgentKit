"""Windows authority for translated Linux hook calls and isolated MCP sessions."""
from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path

from . import codex_hooks, db, hooks_cli
from .config import load_project
from .hook_worktree_paths import write_context
from .leases import Decision, decide
from .secrets import worker_environment
from .wsl_mcp_protocol import CLOSE, METHODS, POLL, SEND
from .wsl_mcp_stream import StreamChannel
from .wsl_transport import HOOK_EVENTS, refusal


def windows_path(value: str) -> str:
    match = re.match(r"^/mnt/([a-z])/(.*)$", value)
    if match:
        return match[1].upper() + ":/" + match[2]
    if value.startswith("/") or "\\" in value or ":" in value:
        raise ValueError("Only normalized drive mounts and relative Linux paths are supported")
    return value


def translate(payload: dict) -> dict:
    result = dict(payload)
    result["cwd"] = windows_path(payload["cwd"])
    tool = dict(payload.get("tool_input") or {})
    for name in ("file_path", "path", "notebook_path", "filePath"):
        if name in tool:
            tool[name] = windows_path(tool[name])
    if isinstance(tool.get("edits"), list):
        tool["edits"] = [{**edit, **{k: windows_path(v) for k, v in edit.items() if k in ("file_path", "path")}}
                         for edit in tool["edits"]]
    result["tool_input"] = tool
    return result


def hook(event: str, payload: dict) -> dict:
    if event not in HOOK_EVENTS or not isinstance(payload, dict):
        return refusal("hook", "[AgentKit] Blocked: unsupported hook")
    try:
        payload = translate(payload)
        output, error = io.StringIO(), io.StringIO()
        # Invoked in a separate interpreter, so redirecting output cannot touch other sessions.
        with redirect_stdout(output), redirect_stderr(error):
            if event == "pre-bash":
                root = Path(os.environ["AGENTKIT_ROOT"])
                task_id = int(os.environ["AGENTKIT_TASK"])
                conn = db.connect(root)
                try:
                    context = write_context(conn, root, task_id, payload)
                    def authorize(raw):
                        raw = windows_path(raw) if raw.startswith("/") else raw
                        relative = context.relative(raw)
                        if relative is None or relative.startswith((".git", ".claude", ".codex", ".ai/runtime")):
                            return Decision(False, "Target is outside the assigned source scope", "outside_worktree")
                        return decide(conn, load_project(root), relative, task_id)
                    command = payload["tool_input"].get("command", "")
                    verdict = codex_hooks._shell(command, authorize, [], worktree=context.worktree, reliable_cwd=True)
                    if verdict.allowed:
                        code = 0
                    else:
                        paths = [context.relative(windows_path(raw) if raw.startswith("/") else raw) or raw for raw in verdict.writes]
                        db.record_violation(conn, task_id, "L4", ", ".join(paths) or command[:200], verdict.reason, channel="shell")
                        sys.stderr.write("[AgentKit] Command blocked. " + verdict.reason)
                        code = 2
                finally:
                    conn.close()
            else:
                if event == "pre-tool-use":
                    targets = hooks_cli._target_paths(payload["tool_input"])
                    if payload.get("tool_name") not in hooks_cli.WRITE_TOOLS or not targets:
                        raise ValueError("Missing or unsupported write-tool targets")
                    for raw in targets:
                        if any(part in raw.replace("\\", "/").split("/") for part in (".git", ".claude", ".codex")):
                            raise ValueError("Worker cannot modify transport or hook control files")
                code = hooks_cli.HANDLERS[event](payload)
        return {"op": "hook", "exit": code, "stdout": output.getvalue(), "stderr": error.getvalue()}
    except Exception as exc:
        return refusal("hook", f"[AgentKit] Blocked: host hook failed closed: {exc}")


class Channel:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = subprocess.Popen([sys.executable, "-I", "-m", "agentkit.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=worker_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages: queue.Queue = queue.Queue()
        def read():
            assert self.proc.stdout
            for line in self.proc.stdout:
                with suppress(ValueError):
                    self.messages.put(json.loads(line))
            self.messages.put(None)
        threading.Thread(target=read, daemon=True).start()

    def call(self, message):
        with self.lock:
            assert self.proc.stdin
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
            if "id" not in message:
                return None
            while True:
                response = self.messages.get(timeout=900)
                if response is None:
                    raise RuntimeError("Host MCP exited")
                if response.get("id") == message["id"]:
                    return response

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


class Dispatcher:
    def __init__(self):
        self.channels: dict[str, Channel | StreamChannel] = {}
        self.lock = threading.Lock()
        self.closed = False

    def handle(self, body):
        if body.get("op") == "hook":
            result = subprocess.run([sys.executable, "-I", "-m", "agentkit.wsl_dispatch", body.get("event", "")],
                input=json.dumps(body.get("payload")), capture_output=True, text=True, encoding="utf-8",
                env=worker_environment(), timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                return refusal("hook", "[AgentKit] Blocked: hook interpreter failed")
            return json.loads(result.stdout)
        if body.get("op") != "mcp" or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("session", ""))):
            raise ValueError("Invalid MCP channel")
        message = body.get("message")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("Invalid JSON-RPC message")
        session, method = body["session"], message.get("method")
        streaming = method in METHODS
        with self.lock:
            if self.closed:
                raise RuntimeError("Host dispatcher closed")
            if method == CLOSE:
                channel = self.channels.pop(session, None)
            else:
                if session not in self.channels:
                    if len(self.channels) >= 2:
                        raise ValueError("Too many MCP channels")
                    self.channels[session] = StreamChannel() if streaming else Channel()
                channel = self.channels[session]
        if method == CLOSE:
            if channel is not None:
                channel.close()
            return {"op": "mcp", "messages": []}
        if isinstance(channel, StreamChannel):
            if not streaming:
                raise ValueError("MCP channel mode differs")
            if method == SEND:
                params = message.get("params")
                channel.send(params.get("message") if isinstance(params, dict) else None)
            return {"op": "mcp", "messages": channel.poll() if method == POLL else []}
        if streaming:
            raise ValueError("MCP channel mode differs")
        assert channel is not None
        return {"op": "mcp", "message": channel.call(message)}

    def close(self):
        with self.lock:
            self.closed = True
            channels = list(self.channels.values())
            self.channels.clear()
        for channel in channels:
            channel.close()


if __name__ == "__main__":
    print(json.dumps(hook(sys.argv[1], json.load(sys.stdin))))
