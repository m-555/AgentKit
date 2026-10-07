"""Opt-in Windows-host / WSL-worker transport: wire format v1 (frozen) and its host end.

Authority runs one way. The Windows host alone opens `tasks.db`, runs the MCP server and gates,
and decides hook verdicts. The Linux worker (`wsl_worker`) runs Linux Claude, never opens
AgentKit state, and multiplexes Claude JSONL lines, unchanged, with tagged control frames on its
stdout. Every frame after the hello carries a per-session HMAC, a sequence number and a direction,
so no process writing into the worker pipes can speak for either side, and replayed or reflected
frames are refused. `HostSession` is the host end, and `wsl_client.Dispatcher` answers its requests.
Stdlib-only, as both sides import it. Not yet wired into the runner, scheduler or registry: see
docs/wsl-transport.md for the remaining seams.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
from dataclasses import dataclass
from typing import IO, Any

VERSION = 1
PREFIX = b"\x1eagentkit-wsl/1 "
MAX_FRAME = 4 * 1024 * 1024
MAX_NATIVE_LINE = 64 * 1024 * 1024
TO_WORKER, TO_HOST = "h2w", "w2h"
SOCKET_ENV = "AGENTKIT_WSL_SOCKET"
HOOK_EVENTS = ("pre-tool-use", "pre-bash", "post-tool-use")
#: Longest wait for an answer, per operation. A hello may shorten these, never extend them.
TIMEOUTS = {"hook": 30.0, "mcp": 900.0}
CONTROL_TIMEOUT = 15.0
#: The only variables a hello may add to the Claude environment. Authority variables never are.
HELLO_ENV = frozenset({"AGENTKIT_MODEL", "AGENTKIT_MODEL_PROFILE", "AGENTKIT_MODEL_EFFORT"})
_FIELDS = frozenset({"v", "dir", "seq", "kind", "id", "body"})


class TransportError(RuntimeError):
    """The channel failed or refused. Anything in flight has an unknown outcome."""


class ProtocolError(TransportError):
    """A frame was malformed, oversized, unauthenticated, replayed or reflected."""


class Overlong(ProtocolError):
    def __init__(self, size: int):
        super().__init__(f"line of {size} bytes exceeds its bound and was drained")
        self.size = size


def dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii")


def _mac(key: bytes, frame: dict[str, Any]) -> str:
    canonical = json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def write(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
        stream.flush()
    except (OSError, ValueError) as exc:
        raise TransportError(f"transport disconnected: {exc}") from exc


class Codec:
    """Authenticated, ordered frames for one end of one session."""

    def __init__(self, key: bytes, *, sends: str):
        if len(key) != 32 or sends not in (TO_WORKER, TO_HOST):
            raise ValueError("a session needs a 32-byte key and a direction")
        self._key, self.sends = key, sends
        self.receives = TO_HOST if sends == TO_WORKER else TO_WORKER
        self.sent = self.seen = 0
        #: Held by every writer of the outgoing stream, so frames and native lines never interleave.
        self.lock = threading.Lock()

    def frame(self, kind: str, body: dict[str, Any] | None = None, ident: str | None = None,
              *, seq: int | None = None) -> bytes:
        frame: dict[str, Any] = {"v": VERSION, "dir": self.sends, "kind": kind, "id": ident,
                                 "seq": self.sent + 1 if seq is None else seq, "body": body or {}}
        frame["mac"] = _mac(self._key, frame)
        data = PREFIX + dumps(frame) + b"\n"
        if len(data) > MAX_FRAME:
            raise ProtocolError(f"{kind} frame of {len(data)} bytes exceeds the {MAX_FRAME}-byte bound")
        return data

    def send(self, stream: IO[bytes], kind: str, body: dict[str, Any] | None = None,
             ident: str | None = None) -> None:
        """Encode and write under one lock, so wire order always equals sequence order."""
        with self.lock:
            write(stream, self.frame(kind, body, ident))
            self.sent += 1

    def write_raw(self, stream: IO[bytes], data: bytes) -> None:
        with self.lock:
            write(stream, data)

    def decode(self, line: bytes) -> dict[str, Any]:
        """Return one verified frame. Nothing unauthenticated is ever returned."""
        if not line.startswith(PREFIX) or len(line) > MAX_FRAME:
            raise ProtocolError("line is not a control frame within the protocol bound")
        try:
            frame = json.loads(line[len(PREFIX):])
        except (ValueError, RecursionError) as exc:
            raise ProtocolError(f"malformed control frame: {exc}") from exc
        mac = frame.pop("mac", None) if isinstance(frame, dict) else None
        if not isinstance(frame, dict) or set(frame) != _FIELDS or frame["v"] != VERSION:
            raise ProtocolError("control frame has unknown fields or version")
        if not (isinstance(mac, str) and mac.isascii() and hmac.compare_digest(mac, _mac(self._key, frame))):
            raise ProtocolError("control frame failed authentication")
        if frame["dir"] != self.receives:
            raise ProtocolError("control frame was reflected back to its sender")
        if type(frame["seq"]) is not int or frame["seq"] <= self.seen:
            raise ProtocolError("control frame was replayed or reordered")
        ident = frame["id"]
        if not isinstance(frame["kind"], str) or not isinstance(frame["body"], dict) or not (
                ident is None or (isinstance(ident, str) and 0 < len(ident) <= 64)):
            raise ProtocolError("control frame metadata is malformed")
        self.seen = frame["seq"]
        return frame


def hello_frame(key: bytes, body: dict[str, Any]) -> bytes:
    """Frame zero, the only frame that carries the key. It is sent before Claude exists."""
    return Codec(key, sends=TO_WORKER).frame("hello", {**body, "key": key.hex()}, seq=0)


def accept_hello(line: bytes | None) -> tuple[Codec, dict[str, Any]]:
    """The worker side of frame zero: its codec and the launch description."""
    data = line or b""
    try:
        frame = json.loads(data[len(PREFIX):]) if data.startswith(PREFIX) else {}
        codec = Codec(bytes.fromhex(frame["body"]["key"]), sends=TO_HOST)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ProtocolError("first frame is not a valid hello") from exc
    codec.seen = -1
    verified = codec.decode(data)
    if verified["kind"] != "hello" or verified["seq"] != 0:
        raise ProtocolError("first frame is not a valid hello")
    body = dict(verified["body"])
    del body["key"]
    return codec, body


def read_line(stream: IO[bytes], limit: int) -> bytes | None:
    """One newline-terminated line, or None at EOF. An overlong line is drained, then raised."""
    line = stream.readline(limit + 1)
    if not line:
        return None
    if limit < len(line) and not line.endswith(b"\n"):
        size = len(line)
        while chunk := stream.readline(65536):
            size += len(chunk)
            if chunk.endswith(b"\n"):
                break
        raise Overlong(size)
    return line


def send_message(sock: socket.socket, value: dict[str, Any]) -> None:
    """One message of the local socket protocol: a bounded JSON object on one line."""
    data = dumps(value) + b"\n"
    if len(data) > MAX_FRAME:
        raise ProtocolError("local message exceeds the protocol bound")
    sock.sendall(data)


def recv_message(sock: socket.socket) -> dict[str, Any]:
    buffer, chunk = bytearray(), b""
    while b"\n" not in chunk:
        chunk = sock.recv(65536)
        if not chunk:
            raise TransportError("local peer closed before a complete message")
        buffer += chunk
        if len(buffer) > MAX_FRAME:
            raise ProtocolError("local message exceeds the protocol bound")
    try:
        value = json.loads(bytes(buffer).split(b"\n", 1)[0])
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"malformed local message: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("local message is not an object")
    return value


def validate_argv(argv: object) -> list[str]:
    """An argument array, never a command line: each item reaches exec as one argument."""
    if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
        raise ValueError("argv must be a list of 1 to 256 strings")
    if not all(isinstance(item, str) and "\x00" not in item and len(item) <= 262144 for item in argv):
        raise ValueError("argv items must be strings without NUL, each at most 256 KiB")
    return list(argv)


@dataclass(frozen=True)
class WorkerHandle:
    """wsl.exe on Windows and the worker inside Linux are different processes, kept apart."""

    host_pid: int
    linux_pid: int
    linux_starttime: int
    boot_id: str
    claude_pid: int
    claude_pgid: int
    claude_starttime: int


@dataclass(frozen=True)
class Liveness:
    alive: bool
    identity_matches: bool
    exit_code: int | None


def refusal(op: object, reason: str) -> dict[str, Any]:
    """A fail-closed answer: a blocking verdict for a hook, an error for anything else."""
    return {"op": "hook", "exit": 2, "stdout": "", "stderr": reason} if op == "hook" else {
        "op": str(op)[:32], "error": reason}


def _identity_ok(body: dict[str, Any]) -> bool:
    worker, claude = body.get("worker"), body.get("claude")
    numbers = [(worker, "pid"), (worker, "starttime"), (claude, "pid"), (claude, "pgid"), (claude, "starttime")]
    return (isinstance(worker, dict) and isinstance(claude, dict) and isinstance(worker.get("boot_id"), str)
            and all(type(part.get(key)) is int for part, key in numbers if isinstance(part, dict)))


