"""Modern subscriptions and stdio replies must flow without a model call."""
import io
import json
import queue
import time

import pytest
from mcp import types

from agentkit import wsl_client, wsl_dispatch, wsl_mcp_stream
from agentkit.wsl_mcp_protocol import CLOSE, POLL, SEND, envelope


def test_open_subscription_does_not_block_tools_or_hide_its_ack():
    if not hasattr(types, "DiscoverRequest"):
        pytest.skip("Installed MCP SDK uses the legacy handshake")
    channel = wsl_mcp_stream.StreamChannel()
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "proxy-regression", "version": "1"}}
    try:
        channel.send({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}})
        discovery = channel.poll()[0]
        assert discovery["id"] == 1 and "result" in discovery
        channel.send({"jsonrpc": "2.0", "id": "listen", "method": "subscriptions/listen",
                      "params": {"_meta": meta, "notifications": {"toolsListChanged": True}}})
        channel.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}})
        received = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
                any(m.get("id") == 2 for m in received) and
                any(m.get("method") == "notifications/subscriptions/acknowledged" for m in received)):
            received.extend(channel.poll())
        ack = next(m for m in received if m.get("method") == "notifications/subscriptions/acknowledged")
        assert ack["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == "listen"
        reply = next(m for m in received if m.get("id") == 2)
        assert any(tool["name"] == "brief" for tool in reply["result"]["tools"])
    finally:
        channel.close()
    assert channel.proc.poll() is not None


def test_legacy_initialize_still_exposes_tools_on_the_duplex_proxy():
    channel = wsl_mcp_stream.StreamChannel()
    try:
        channel.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "legacy-regression", "version": "1"}}})
        assert "result" in channel.poll()[0]
        channel.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        channel.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        reply = channel.poll()[0]
        assert reply["id"] == 2 and any(t["name"] == "brief" for t in reply["result"]["tools"])
    finally:
        channel.close()


class Input:
    def __init__(self):
        self.lines = queue.Queue()
    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line


def test_linux_proxy_forwards_notifications_responses_and_cleans_up(monkeypatch):
    source, output, incoming = Input(), io.StringIO(), queue.Queue()
    source.lines.put(json.dumps({"jsonrpc": "2.0", "id": "listen", "method": "subscriptions/listen"}))
    source.lines.put(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}))
    closed, sessions, messages = [], set(), []
    def request(body):
        sessions.add(body["session"])
        command = body["message"]
        if command["method"] == SEND:
            native = command["params"]["message"]
            if native["method"] == "subscriptions/listen":
                incoming.put({"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged"})
            else:
                incoming.put({"jsonrpc": "2.0", "id": 9, "result": {"tools": [{"name": "brief"}]}})
        elif command["method"] == CLOSE:
            closed.append(True)
        elif command["method"] == POLL:
            try:
                messages.append(incoming.get(timeout=0.05))
                if messages[-1].get("id") == 9:
                    source.lines.put(None)
                return {"messages": [messages[-1]]}
            except queue.Empty:
                pass
        return {"messages": []}
    monkeypatch.setattr(wsl_client, "request", request)
    wsl_client.proxy(source, output)
    assert [json.loads(line) for line in output.getvalue().splitlines()] == messages
    assert messages[0]["method"] == "notifications/subscriptions/acknowledged"
    assert messages[1]["id"] == 9
    assert closed == [True] and len(sessions) == 1


def test_linux_proxy_fails_on_host_loss_without_waiting_for_stdin(monkeypatch):
    source, closed = Input(), []
    def request(body):
        if body["message"]["method"] == CLOSE:
            closed.append(True)
            return {}
        raise RuntimeError("host disconnected")
    monkeypatch.setattr(wsl_client, "request", request)
    try:
        with pytest.raises(RuntimeError, match="host disconnected"):
            wsl_client.proxy(source, io.StringIO())
    finally:
        source.lines.put(None)
    assert closed == [True]


def test_closed_clients_release_the_channel_cap_without_unbounding_it(monkeypatch):
    class Channel:
        def __init__(self):
            self.closed = False
        def send(self, message):
            pass
        def poll(self):
            return []
        def close(self):
            self.closed = True
    monkeypatch.setattr(wsl_dispatch, "StreamChannel", Channel)
    dispatcher = wsl_dispatch.Dispatcher()
    def operation(session, method):
        return dispatcher.handle({"op": "mcp", "session": session, "message": envelope(method)})
    try:
        operation("a" * 32, POLL)
        operation("b" * 32, POLL)
        first = dispatcher.channels["a" * 32]
        with pytest.raises(ValueError, match="Too many"):
            operation("c" * 32, POLL)
        operation("a" * 32, CLOSE)
        assert first.closed and len(dispatcher.channels) == 1
        operation("c" * 32, POLL)
        assert len(dispatcher.channels) == 2
        with pytest.raises(ValueError, match="mode differs"):
            dispatcher.handle({"op": "mcp", "session": "b" * 32,
                               "message": {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}})
    finally:
        dispatcher.close()


def test_dispatcher_shutdown_closes_clients_and_refuses_late_requests():
    dispatcher = wsl_dispatch.Dispatcher()
    dispatcher.handle({"op": "mcp", "session": "a" * 32, "message": envelope(POLL)})
    process = dispatcher.channels["a" * 32].proc
    dispatcher.close()
    assert process.poll() is not None and not dispatcher.channels
    with pytest.raises(RuntimeError, match="dispatcher closed"):
        dispatcher.handle({"op": "mcp", "session": "b" * 32, "message": envelope(POLL)})


@pytest.mark.parametrize("output,reason", [
    ('{"broken":true}\n', "Invalid MCP output"),
    ('x' * 70 + '\n', "frame bound"),
    ('{"jsonrpc":"2.0","method":"notification"}\n' * 33, "backlog exceeded"),
])
def test_malformed_oversized_or_flooding_server_output_fails_closed(monkeypatch, output, reason):
    class Process:
        stdin = io.StringIO()
        stdout = io.StringIO(output)
    monkeypatch.setattr(wsl_mcp_stream.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(wsl_mcp_stream, "MAX_FRAME", 64)
    channel = wsl_mcp_stream.StreamChannel()
    assert channel.finished.wait(1)
    with pytest.raises(RuntimeError, match=reason):
        channel.poll()
