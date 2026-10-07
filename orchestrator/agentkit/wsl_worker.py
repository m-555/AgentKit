"""Linux worker: never opens AgentKit databases; host disconnect stops its process group."""
from __future__ import annotations

import os
import queue
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from secrets import token_hex
from typing import IO, Any

from .wsl_runtime import (
    HOST_LOSS_GRACE,
    MAX_CLIENTS,
    Launch,
    _clamp,
    _cleanup,
    _exit_status,
    _listen,
    _signal_group,
    boot_id,
    child_environment,
    descends_from,
    measure,
    starttime,
)
from .wsl_transport import (
    MAX_FRAME,
    MAX_NATIVE_LINE,
    PREFIX,
    TIMEOUTS,
    Codec,
    Overlong,
    ProtocolError,
    TransportError,
    accept_hello,
    read_line,
    recv_message,
    send_message,
)


class Worker:
    """One Claude session inside WSL, supervised entirely through the host channel."""

    def __init__(self, codec: Codec, launch: Launch, host_in: IO[bytes], host_out: IO[bytes]):
        self.codec, self.launch, self.host_in, self.host_out = codec, launch, host_in, host_out
        self.child: subprocess.Popen[bytes] | None = None
        self.identity: dict[str, Any] = {}
        self.refusing = ""
        self.violations = 0
        self._pending: dict[str, tuple[str, queue.Queue[dict[str, Any] | None]]] = {}
        self._lock, self._group = threading.Lock(), threading.Lock()
        self._reaped = False
        self._done = threading.Event()

    def send(self, kind: str, body: dict[str, Any] | None = None, ident: str | None = None) -> bool:
        try:
            self.codec.send(self.host_out, kind, body, ident)
        except TransportError:
            return False
        return True

    def run(self) -> int:
        folder = tempfile.mkdtemp(prefix="agentkit-wsl-")
        path = os.path.join(folder, "s")
        server: socket.socket | None = None
        try:
            server = _listen(path)
            runtime = measure(self.launch.argv[0])
            child = subprocess.Popen(self.launch.argv, cwd=self.launch.cwd, start_new_session=True,
                                     env=child_environment(dict(os.environ), self.launch.env, path),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.send("exited", {"code": 127, "error": str(exc)[:500]})
            _cleanup(server, path, folder)
            return 127
        self.child, mine = child, os.getpid()
        self.identity = {"worker": {"pid": mine, "starttime": starttime(mine), "boot_id": boot_id()},
                         "claude": {"pid": child.pid, "pgid": child.pid, "starttime": starttime(child.pid)},
                         "runtime": runtime}
        self.send("ready", self.identity)
        native = threading.Thread(target=self._native, args=(child,), daemon=True)
        for thread in (native, threading.Thread(target=self._feed, args=(child,), daemon=True),
                       threading.Thread(target=self._errors, args=(child,), daemon=True),
                       threading.Thread(target=self._read_host, daemon=True),
                       threading.Thread(target=self._serve, args=(server,), daemon=True)):
            thread.start()
        while _exit_status(child.pid) is None:
            time.sleep(0.05)
        code = self._stop_group(0.0)
        native.join(timeout=5)
        self._done.set()
        self.send("exited", {"code": code})
        _cleanup(server, path, folder)
        return code if code >= 0 else 128 - code

    def _feed(self, child: subprocess.Popen[bytes]) -> None:
        stream = child.stdin
        assert stream is not None
        with suppress(OSError, ValueError), stream:
            stream.write((self.launch.prompt or "").encode("utf-8", "replace"))

    def _native(self, child: subprocess.Popen[bytes]) -> None:
        """Copy Claude stdout unchanged. Only a line that could pose as a control frame is wrapped."""
        stream = child.stdout
        assert stream is not None
        with suppress(OSError, ValueError, TransportError):
            while True:
                try:
                    line = read_line(stream, MAX_NATIVE_LINE)
                except Overlong as exc:
                    self.send("notice", {"dropped_native_bytes": exc.size})
                    continue
                if line is None:
                    return
                line = line if line.endswith(b"\n") else line + b"\n"
                if not line.startswith(PREFIX[:1]):
                    self.codec.write_raw(self.host_out, line)
                elif not self.send("native", {"line": line.decode("utf-8", "replace")}):
                    self.send("notice", {"dropped_native_bytes": len(line)})

    def _errors(self, child: subprocess.Popen[bytes]) -> None:
        """Claude stderr goes to this stderr, which wsl.exe hands to the host unchanged."""
        stream = child.stderr
        assert stream is not None
        with suppress(OSError, ValueError):
            while chunk := os.read(stream.fileno(), 65536):
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()

    def _read_host(self) -> None:
        with suppress(OSError, ValueError):
            while True:
                try:
                    line = read_line(self.host_in, MAX_FRAME)
                    if line is None:
                        break
                    frame = self.codec.decode(line)
                except ProtocolError as exc:
                    self._violation(str(exc), compromise=True)
                    continue
                kind, ident, body = frame["kind"], frame["id"], frame["body"]
                if kind == "reply" and ident:
                    self._deliver(ident, body)
                elif kind == "probe" and ident:
                    self.send("alive", self._liveness(), ident)
                elif kind == "terminate" and ident:
                    threading.Thread(target=self._terminate, args=(ident, body), daemon=True).start()
                else:
                    self._violation(f"unexpected {kind} frame from the host", compromise=True)
        self._host_lost()

    def _host_lost(self) -> None:
        """Nothing can supervise Claude without the host: waiting requests fail and the group stops."""
        with self._lock:
            self.refusing = self.refusing or "host disconnected"
            waiting, self._pending = list(self._pending.values()), {}
        for _, waiter in waiting:
            waiter.put(None)
        if not self._done.is_set():
            self._stop_group(HOST_LOSS_GRACE)

    def _deliver(self, ident: str, body: dict[str, Any]) -> None:
        with self._lock:
            entry = self._pending.pop(ident, None)
        if entry is None:
            self._violation(f"reply {ident} answers no waiting request", compromise=False)
            return
        op, waiter = entry
        if body.get("op") != op:
            self._violation(f"reply {ident} does not match its {op} request", compromise=False)
            body = {"op": op, "error": "the host reply did not match the request, refused"}
        waiter.put(body)

    def forward(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one local request to the host, and wait, bounded, for the reply with its id."""
        op = request.get("op")
        if not isinstance(op, str) or op not in TIMEOUTS:
            return {"ok": False, "error": "operation is not served"}
        ident, limit = token_hex(8), self.launch.timeouts[op]
        waiter: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self._lock:
            if self.refusing:
                return {"ok": False, "error": f"{self.refusing}, refused (fail closed)"}
            self._pending[ident] = (op, waiter)
        body: dict[str, Any] = {key: request[key] for key in ("op", "event", "payload", "session", "message") if key in request}
        try:
            if not self.send("request", body, ident):
                return {"ok": False, "error": "the request could not be sent to the host, outcome unknown"}
            reply = waiter.get(timeout=limit)
        except queue.Empty:
            return {"ok": False, "error": f"the host gave no answer within {limit:g}s, outcome unknown"}
        finally:
            with self._lock:
                self._pending.pop(ident, None)
        if reply is None:
            return {"ok": False, "error": "the host disconnected, outcome unknown"}
        return {"ok": True, "body": reply}

    def _liveness(self) -> dict[str, Any]:
        child, claude, mine = self.child, dict(self.identity["claude"]), os.getpid()
        assert child is not None
        with self._group:
            code = child.returncode if self._reaped else _exit_status(child.pid)
            if code is None:
                try:
                    claude["starttime"] = starttime(child.pid)
                except (OSError, ValueError, IndexError):
                    claude["starttime"] = -1
        return {"worker": {"pid": mine, "starttime": starttime(mine), "boot_id": boot_id()},
                "claude": {**claude, "running": code is None, "exit": code}, "violations": self.violations}

    def _terminate(self, ident: str, body: dict[str, Any]) -> None:
        claude, worker = self.identity["claude"], self.identity["worker"]
        if (body.get("pgid"), body.get("starttime"), body.get("boot_id")) != (
                claude["pgid"], claude["starttime"], worker["boot_id"]):
            self.send("refused", {"reason": "termination names a different process group"}, ident)
            return
        try:
            grace = _clamp(body.get("grace", 10.0), 0.0, 60.0)
        except ValueError:
            grace = 10.0
        self.send("terminated", {"code": self._stop_group(grace)}, ident)

    def _stop_group(self, grace: float) -> int:
        """Signal the Claude process group, then reap its leader, always in that order."""
        child = self.child
        assert child is not None
        with self._group:
            if not self._reaped:
                _signal_group(child.pid, signal.SIGTERM)
                deadline = time.monotonic() + grace
                while _exit_status(child.pid) is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                _signal_group(child.pid, signal.SIGKILL)  # type: ignore[attr-defined]  # Linux-only runtime.
                child.wait()
                self._reaped = True
            return int(child.returncode)

    def _violation(self, reason: str, *, compromise: bool) -> None:
        self.violations += 1
        if compromise:
            self.refusing = "transport integrity failure"
        if self.violations <= 100:
            self.send("notice", {"violation": reason[:500], "refusing": bool(self.refusing)})

    def _serve(self, server: socket.socket) -> None:
        slots = threading.BoundedSemaphore(MAX_CLIENTS)
        while not self._done.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if slots.acquire(blocking=False):
                threading.Thread(target=self._client, args=(conn, slots), daemon=True).start()
            else:
                conn.close()

    def _client(self, conn: socket.socket, slots: threading.BoundedSemaphore) -> None:
        try:
            with conn, suppress(OSError, TransportError):
                conn.settimeout(10.0)
                problem = self._peer_problem(conn)
                send_message(conn, {"ok": False, "error": problem} if problem else self.forward(recv_message(conn)))
        finally:
            slots.release()

    def _peer_problem(self, conn: socket.socket) -> str:
        """Only this user, and only processes descended from Claude, may ask anything."""
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))  # type: ignore[attr-defined]  # Linux-only runtime.
        pid, uid, _ = struct.unpack("3i", raw)
        if uid != os.getuid():  # type: ignore[attr-defined]  # Linux-only runtime.
            return "peer belongs to another user"
        if self.child is None or not descends_from(pid, self.child.pid):
            return "peer is outside this worker Claude process tree"
        return ""


def main() -> int:
    if not sys.platform.startswith("linux"):
        sys.stderr.write("[agentkit-wsl] the worker runs only inside Linux\n")
        return 2
    try:
        codec, hello = accept_hello(read_line(sys.stdin.buffer, MAX_FRAME))
        launch = Launch.from_hello(hello)
    except (TransportError, ValueError) as exc:
        sys.stderr.write(f"[agentkit-wsl] hello refused: {exc}\n")
        return 2
    return Worker(codec, launch, sys.stdin.buffer.raw, sys.stdout.buffer).run()


if __name__ == "__main__":
    raise SystemExit(main())
