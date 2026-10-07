"""Unsupported existing-VS-Code IPC diagnostic; no standalone model sessions."""
from __future__ import annotations

import json
import os
import struct
import time
import uuid
from pathlib import Path


def peer_process(stream) -> dict:
    """Bind the pipe to Code.exe and its creation time, with lazy Windows imports."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.ULONG()
    if not kernel.GetNamedPipeServerProcessId(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if Path(name.value).name.lower() != "code.exe":
            raise RuntimeError("Existing pipe is not served by Code.exe")
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *[ctypes.byref(value) for value in stamps]):
            raise ctypes.WinError(ctypes.get_last_error())
        created = stamps[0].dwLowDateTime | (stamps[0].dwHighDateTime << 32)
        return {"pid": pid.value, "executable": name.value, "created_filetime": created}
    finally:
        kernel.CloseHandle(process)


class Connection:
    """One bounded connection to the already-running private IPC owner; no reconnect."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Native IPC diagnostic requires Windows and an existing VS Code chat")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self.stream = open(r"\\.\pipe\codex-ipc", "r+b", buffering=0)  # noqa: SIM115 - owned by close()
        try:
            self.peer = peer_process(self.stream)
            self.client = None
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                                 wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            self.kernel.PeekNamedPipe.restype = wintypes.BOOL
            self.handle = msvcrt.get_osfhandle(self.stream.fileno())
        except Exception:
            self.stream.close()
            raise

    def current_peer(self) -> dict:
        return peer_process(self.stream)

    def exact(self, size, deadline):
        import ctypes
        from ctypes import wintypes

        result = bytearray()
        while len(result) < size:
            if time.monotonic() >= deadline:
                raise TimeoutError("Bounded native IPC read timed out")
            available = wintypes.DWORD()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not available.value:
                time.sleep(0.05)
                continue
            part = self.stream.read(min(available.value, size - len(result)))
            if not part:
                raise EOFError("Existing native IPC connection closed")
            result.extend(part)
        return bytes(result)

    def send(self, payload):
        data = json.dumps(payload).encode("utf-8")
        if len(data) > 16 * 1024 * 1024:
            raise RuntimeError("Native IPC frame exceeded its bound")
        packet = struct.pack("<I", len(data)) + data
        if self.stream.write(packet) != len(packet):
            raise OSError("Native IPC write incomplete; outcome unknown")

    def next(self, deadline):
        size = struct.unpack("<I", self.exact(4, deadline))[0]
        if size > 16 * 1024 * 1024:
            raise RuntimeError("Native IPC frame exceeded its bound")
        message = json.loads(self.exact(size, deadline))
        if not isinstance(message, dict):
            raise RuntimeError("Native IPC message schema unknown")
        return message

    def request(self, method, version, params, owner=None):
        identifier = str(uuid.uuid4())
        payload = {"type": "request", "requestId": identifier, "method": method,
                   "version": version, "params": params, "timeoutMs": 15000}
        if self.client:
            payload["sourceClientId"] = self.client
        if owner:
            payload["targetClientId"] = owner
        self.send(payload)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            message = self.next(deadline)
            if message.get("type") == "response" and message.get("requestId") == identifier:
                return message
        raise TimeoutError("Native request outcome unknown; no delivery retry")

    def initialize(self):
        response = self.request("initialize", 0, {"clientType": "agentkit-wake-diagnostic"})
        result = response.get("result") or {}
        if response.get("resultType") != "success" or not result.get("clientId"):
            raise RuntimeError("Native IPC initialization rejected")
        self.client = result["clientId"]

    def discover_owner(self, thread):
        response = self.request("thread-owner-discovery", 1, {"hostId": "local", "conversationId": thread})
        if response.get("resultType") != "success" or not response.get("handledByClientId"):
            raise RuntimeError("Existing native chat ownership not confirmed")
        return response["handledByClientId"]

    def following(self, owner, thread, enabled):
        self.send({"type": "broadcast", "method": "thread-stream-following-changed",
                   "version": 1, "sourceClientId": self.client, "targetClientIds": [owner],
                   "params": {"conversationId": thread, "hostId": "local", "following": enabled}})

    def snapshot(self, owner, thread):
        self.following(owner, thread, True)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            message = self.next(deadline)
            params = message.get("params") or {}
            change = params.get("change") or {}
            if (message.get("type") == "broadcast" and message.get("method") == "thread-stream-state-changed"
                    and message.get("version") == 11 and message.get("sourceClientId") == owner
                    and self.client in message.get("targetClientIds", [])
                    and params.get("conversationId") == thread and params.get("hostId") == "local"
                    and change.get("type") == "snapshot"):
                return change["conversationState"]
        raise TimeoutError("No matching native chat snapshot")

    def close(self):
        self.stream.close()


def overview(state) -> dict:
    """Extract only authority/idle fences; never persist conversation text."""
    if not isinstance(state, dict) or not isinstance(state.get("requests"), list):
        raise RuntimeError("Native request schema unknown; refusing wake")
    unconfirmed = state.get("unconfirmedTurnSubmissions", [])
    if not isinstance(unconfirmed, list):
        raise RuntimeError("Native unconfirmed input schema unknown; refusing wake")
    turns = state.get("turns") or []
    if not isinstance(turns, list):
        raise RuntimeError("Native turn schema unknown; refusing wake")
    latest = turns[-1] if turns else {}
    history = state.get("turnHistory") or {}
    if history.get("kind") == "canonical":
        canonical = history.get("history") or {}
        entities = canonical.get("entitiesByKey") or {}
        islands = canonical.get("islands") or []
        latest = {}
        turns = list(entities)
        if islands and islands[-1].get("newerBoundary", {}).get("status") == "exhausted":
            entries = islands[-1].get("entries") or []
            if entries:
                latest = entities.get(entries[-1].get("value"), {})
    runtime = state.get("threadRuntimeStatus")
    if not isinstance(runtime, dict) or not isinstance(latest, dict):
        raise RuntimeError("Native runtime schema unknown; refusing wake")
    turn_id = latest.get("turnId")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise RuntimeError("Native latest turn identity unknown; refusing wake")
    pending = bool(state["requests"] or unconfirmed or any(
        state.get(key) for key in state if "pending" in key.lower() or "unconfirmed" in key.lower()))
    return {"runtime": runtime.get("type"), "pending": pending, "turn_count": len(turns),
            "latest_turn_id": latest.get("turnId"), "latest_status": latest.get("status"),
            "capacity_failure": isinstance(latest.get("error"), dict)
            and latest["error"].get("codexErrorInfo") == "serverOverloaded",
            "quota_failure": isinstance(latest.get("error"), dict)
            and latest["error"].get("codexErrorInfo") == "usageLimitExceeded"}
