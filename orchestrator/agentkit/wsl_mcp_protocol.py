"""Private MCP proxy envelopes inside the existing authenticated v1 wire."""

SEND = "agentkit/transport/send"
POLL = "agentkit/transport/poll"
CLOSE = "agentkit/transport/close"
METHODS = frozenset((SEND, POLL, CLOSE))


def envelope(method: str, message: dict | None = None) -> dict:
    if method not in METHODS:
        raise ValueError("Unknown MCP proxy operation")
    return {"jsonrpc": "2.0", "method": method,
            "params": {"message": message} if message is not None else {}}
