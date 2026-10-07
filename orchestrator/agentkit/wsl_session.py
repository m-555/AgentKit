"""Authenticated Windows endpoint of one Linux worker session."""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from secrets import token_bytes, token_hex
from typing import IO, Any

from .wsl_transport import (
    CONTROL_TIMEOUT,
    HELLO_ENV,
    MAX_NATIVE_LINE,
    PREFIX,
    TO_WORKER,
    Codec,
    Liveness,
    ProtocolError,
    TransportError,
    WorkerHandle,
    _identity_ok,
    hello_frame,
    read_line,
    refusal,
    validate_argv,
)


class HostSession:
    """The Windows end of one worker channel. It holds no database connection.

    `on_native` gets every Claude JSONL line exactly as Claude wrote it. `serve`, normally
    `wsl_client.Dispatcher.handle`, answers worker requests, at most `max_parallel` at once.
    After any integrity failure, later requests are refused rather than served.
    """

    def __init__(self, to_worker: IO[bytes], from_worker: IO[bytes], *, on_native: Callable[[str], None],
                 serve: Callable[[dict[str, Any]], dict[str, Any]] | None = None, key: bytes | None = None,
                 max_parallel: int = 8):
        self._key = key or token_bytes(32)
        self.codec = Codec(self._key, sends=TO_WORKER)
        self._out, self._in, self.on_native, self.serve = to_worker, from_worker, on_native, serve
        self.identity: dict[str, Any] | None = None
        self.exit_code: int | None = None
        self.violations: list[str] = []
        self.notices: list[dict[str, Any]] = []
        self.compromised = False
        self.closed, self._ready = threading.Event(), threading.Event()
        self._pending: dict[str, queue.Queue[dict[str, Any] | None]] = {}
        self._lock, self._slots = threading.Lock(), threading.BoundedSemaphore(max_parallel)

    def start(self, argv: list[str], cwd: str, *, prompt: str | None = None, env: dict[str, str] | None = None,
              timeouts: dict[str, float] | None = None, wait: float = CONTROL_TIMEOUT) -> dict[str, Any]:
        """Send the hello, then wait for the identity the worker measured."""
        if not set(env or {}) <= HELLO_ENV:
            raise ValueError(f"hello env may only set {sorted(HELLO_ENV)}")
        body = {"argv": validate_argv(argv), "cwd": cwd, "prompt": prompt, "env": env or {}, "timeouts": timeouts or {}}
        self.codec.write_raw(self._out, hello_frame(self._key, body))
        threading.Thread(target=self._pump, daemon=True).start()
        if not self._ready.wait(wait) or self.identity is None:
            raise TransportError("worker reported no valid identity, outcome unknown")
        return self.identity

    def _pump(self) -> None:
        try:
            with suppress(OSError, ValueError):
                while True:
                    try:
                        line = read_line(self._in, MAX_NATIVE_LINE)
                        if line is None:
                            break
                        if line.startswith(PREFIX[:1]):
                            self._route(self.codec.decode(line))
                        else:
                            self.on_native(line.decode("utf-8", "replace"))
                    except ProtocolError as exc:
                        self._violation(str(exc))
        finally:
            self.closed.set()
            self._ready.set()
            with self._lock:
                waiting, self._pending = list(self._pending.values()), {}
            for waiter in waiting:
                waiter.put(None)

    def _route(self, frame: dict[str, Any]) -> None:
        kind, ident, body = frame["kind"], frame["id"], frame["body"]
        if kind == "ready" and self.identity is None:
            if _identity_ok(body):
                self.identity = body
            else:
                self._violation("ready frame carries a malformed identity")
            self._ready.set()
        elif kind == "native" and isinstance(body.get("line"), str):
            self.on_native(body["line"])
        elif kind == "request" and ident:
            if self._slots.acquire(blocking=False):
                threading.Thread(target=self._answer, args=(ident, body), daemon=True).start()
            else:
                self._reply(ident, refusal(body.get("op"), "[AgentKit] Blocked: the host is busy."))
        elif kind in ("alive", "terminated", "refused") and ident:
            with self._lock:
                waiter = self._pending.pop(ident, None)
            if waiter is not None:
                waiter.put(frame)
            else:  # a late answer to a request that already timed out here
                self.notices = [*self.notices, {"late": kind, "id": ident}][-100:]
        elif kind == "exited" and type(body.get("code")) is int:
            self.exit_code = body["code"]
        elif kind == "notice":
            self.notices = [*self.notices, body][-100:]
        else:
            self._violation(f"unexpected {kind} frame from the worker")

    def _answer(self, ident: str, body: dict[str, Any]) -> None:
        try:
            if self.compromised or self.serve is None:
                reply = refusal(body.get("op"), "[AgentKit] Blocked: transport integrity failure (fail closed).")
            else:
                reply = self.serve(body)
        except Exception as exc:  # a dispatcher bug must still answer, and answer closed
            reply = refusal(body.get("op"), f"[AgentKit] Blocked: the host dispatcher failed ({exc}).")
        finally:
            self._slots.release()
        self._reply(ident, reply)

    def _reply(self, ident: str, body: dict[str, Any]) -> None:
        try:
            self.codec.send(self._out, "reply", body, ident)
        except ProtocolError:  # too large: answer closed rather than leave the request to time out
            with suppress(TransportError):
                self.codec.send(self._out, "reply", refusal(body.get("op"), "reply exceeds the frame bound"), ident)
        except TransportError:
            pass

    def _violation(self, reason: str) -> None:
        self.compromised = True
        self.violations = [*self.violations, reason[:500]][-100:]

    def _ask(self, kind: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        ident = token_hex(8)
        waiter: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self._lock:
            if self.closed.is_set():
                raise TransportError("worker channel is closed")
            self._pending[ident] = waiter
        try:
            self.codec.send(self._out, kind, body, ident)
            frame = waiter.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"worker gave no {kind} answer within {timeout:g}s") from None
        finally:
            with self._lock:
                self._pending.pop(ident, None)
        if frame is None:
            raise TransportError("worker channel closed before it answered")
        return frame

    def _known(self) -> dict[str, Any]:
        if self.identity is None:
            raise TransportError("worker never reported its identity")
        return self.identity

    def probe(self, timeout: float = CONTROL_TIMEOUT) -> Liveness:
        """Ask, by authenticated frame, whether the exact processes reported at start still run."""
        known, frame = self._known(), self._ask("probe", {}, timeout)
        body = frame["body"]
        claude = body["claude"] if isinstance(body.get("claude"), dict) else {}
        same = frame["kind"] == "alive" and body.get("worker") == known["worker"] and all(
            claude.get(key) == known["claude"][key] for key in ("pid", "pgid", "starttime"))
        code = claude.get("exit")
        return Liveness(same and claude.get("running") is True, same, code if type(code) is int else None)

    def terminate(self, grace: float = 10.0, timeout: float | None = None) -> int | None:
        """Stop exactly the Claude process group this session started, and nothing else in WSL."""
        claude, worker = self._known()["claude"], self._known()["worker"]
        named = {"pgid": claude["pgid"], "starttime": claude["starttime"], "boot_id": worker["boot_id"], "grace": grace}
        frame = self._ask("terminate", named, timeout or grace + CONTROL_TIMEOUT)
        reason, code = frame["body"].get("reason"), frame["body"].get("code")
        if frame["kind"] != "terminated":
            raise TransportError(f"worker refused termination: {reason}")
        return code if type(code) is int else None

    def handle(self, host_pid: int) -> WorkerHandle:
        """Both process identities this session spans, kept apart."""
        worker, claude = self._known()["worker"], self._known()["claude"]
        return WorkerHandle(host_pid, worker["pid"], worker["starttime"], worker["boot_id"],
                            claude["pid"], claude["pgid"], claude["starttime"])

    def close(self) -> None:
        """End the session from the host. The worker treats that as host loss and stops its group."""
        with self.codec.lock, suppress(OSError, ValueError):
            self._out.close()
