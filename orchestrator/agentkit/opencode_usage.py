"""Normalize only explicit OpenCode step usage; never inspect message content."""
from __future__ import annotations


def _number(value):
    return value if type(value) is int and value >= 0 else None


def consume(state, event, merge):
    if event.get("type") == "step_start":
        state.terminal = False
        return
    if state.provider and state.provider != "opencode":
        state.partial = True
    state.provider = "opencode"
    part = event.get("part")
    if not isinstance(part, dict) or not isinstance(part.get("tokens"), dict):
        state.partial = True
        state.terminal = False
        return
    raw = part["tokens"]
    cache = raw.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    supplied = {
        "input_tokens": raw.get("input"), "output_tokens": raw.get("output"),
        "thinking_tokens": raw.get("reasoning"), "cached_input_tokens": cache.get("read"),
        "cache_write_input_tokens": cache.get("write"),
    }
    values = {name: _number(value) for name, value in supplied.items()}
    state.partial |= any(value is not None and values[name] is None
                         for name, value in supplied.items())
    identifier = part.get("id")
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
        state.partial = True
        state.sequence += 1
        identifier = f"anonymous:{state.sequence}"
    session = event.get("sessionID")
    session = session if isinstance(session, str) and len(session) <= 256 else ""
    merge(state, state.opencode, f"{session}:{identifier}", values)
    state.agent_turns = len(state.opencode)
    state.terminal = part.get("reason") == "stop"
