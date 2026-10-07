"""Read bounded public usage metadata; never infer tokens from message content.

input_tokens is the provider's reported value: Claude excludes cache reads and
writes, Codex includes cached input. Claude result totals supersede message
snapshots. Missing counters remain None, including thinking and cache writes.
complete describes an observed terminal stream, not availability of every
optional counter. Partial tails, malformed records and anonymous Claude
snapshots are explicitly incomplete. No model calls or database access occur.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

MAX_READ_BYTES = 1_048_576
MAX_LINE_BYTES = 262_144
MAX_CACHE_FILES = 64
MAX_USAGE_RECORDS = 2_048
_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens",
           "cached_input_tokens", "cache_write_input_tokens")
_CACHE: OrderedDict[tuple[str, int], _State] = OrderedDict()
_LOCK = threading.Lock()


@dataclass
class _State:
    identity: tuple = ()
    size: int = 0
    mtime: int = 0
    offset: int = 0
    anchor: bytes = b""
    pending: bytes = b""
    dropping: bool = False
    partial: bool = False
    terminal: bool = False
    provider: str = ""
    sequence: int = 0
    active_turn: str | None = None
    claude: dict = field(default_factory=dict)
    codex: dict = field(default_factory=dict)
    result: dict | None = None
    agent_turns: int | None = None


def _unknown(source: str) -> dict:
    return {**dict.fromkeys(_FIELDS), "source": source, "complete": False}


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", None))


def _object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, (str, bytes)):
        return {}
    try:
        result = json.loads(value)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def _number(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage(raw: dict, provider: str) -> tuple[dict, bool]:
    names = {
        "input_tokens": "input_tokens", "output_tokens": "output_tokens",
        "cached_input_tokens": ("cache_read_input_tokens" if provider == "claude"
                                else "cached_input_tokens"),
        "cache_write_input_tokens": ("cache_creation_input_tokens" if provider == "claude"
                                     else "cache_write_input_tokens"),
    }
    values = {name: _number(raw.get(key)) for name, key in names.items()}
    candidates = [raw[key] for key in ("thinking_tokens", "reasoning_tokens",
                                      "reasoning_output_tokens") if key in raw]
    details = raw.get("output_tokens_details")
    if isinstance(details, dict):
        candidates.extend(details[key] for key in ("thinking_tokens", "reasoning_tokens")
                          if key in details)
    valid = [_number(value) for value in candidates]
    values["thinking_tokens"] = (max(cast(list[int], valid))
                                if valid and all(v is not None for v in valid) else None)
    bad = any(key in raw and _number(raw[key]) is None for key in names.values())
    return values, bad or any(value is None for value in valid)


def _identifier(event: dict, *names: str) -> str | None:
    for name in names:
        value = event.get(name)
        if isinstance(value, str) and 0 < len(value) <= 256:
            return value
        if type(value) is int and value >= 0:
            return str(value)
    return None


def _merge(state: _State, rows: dict, key: str, values: dict) -> None:
    if key not in rows:
        if len(rows) >= MAX_USAGE_RECORDS:
            state.partial = True
            return
        rows[key] = values
        return
    previous = rows[key]
    for name, value in values.items():
        if value is not None:
            previous[name] = max(previous[name], value) if previous[name] is not None else value


def _event(state: _State, event: dict) -> None:
    kind = event.get("type")
    if kind in ("turn.started", "thread.started"):
        state.terminal = False
        if kind == "turn.started":
            state.sequence += 1
            turn = event.get("turn")
            identifier = _identifier(event, "turn_id", "id", "uuid")
            if not identifier and isinstance(turn, dict):
                identifier = _identifier(turn, "id")
            state.active_turn = identifier or f"sequence:{state.sequence}"
        return
    if kind in ("turn.failed", "error", "stream_event"):
        state.terminal = False
        return
    if kind == "system" and event.get("subtype") == "init":
        state.terminal = False
        return
    if kind not in ("assistant", "result", "turn.completed"):
        return
    provider = "codex" if kind == "turn.completed" else "claude"
    if state.provider and state.provider != provider:
        state.partial = True
    state.provider = provider
    state.terminal = kind in ("result", "turn.completed")
    message = event.get("message") if kind == "assistant" else event
    if not isinstance(message, dict):
        state.partial = True
        return
    raw = message.get("usage")
    if not isinstance(raw, dict):
        if state.terminal:
            state.partial = True
        return
    values, bad = _usage(raw, provider)
    state.partial |= bad
    if kind == "result":
        state.result = values
        state.agent_turns = _number(event.get("num_turns"))
    elif kind == "assistant":
        key = _identifier(message, "id") or _identifier(event, "uuid")
        if not key:
            # Without identity an updated snapshot cannot be deduplicated safely.
            state.partial = True
            state.sequence += 1
            key = f"anonymous:{state.sequence}"
        _merge(state, state.claude, key, values)
    else:
        turn = event.get("turn")
        key = _identifier(event, "turn_id", "id", "uuid")
        if not key and isinstance(turn, dict):
            key = _identifier(turn, "id")
        key = key or state.active_turn
        if not key:
            state.sequence += 1
            key = f"anonymous:{state.sequence}"
        _merge(state, state.codex, key, values)


def _line(state: _State, line: bytes) -> None:
    record = _object(line)
    if not record:
        state.partial = True
        return
    if record.get("channel") == "stderr":
        return
    wrapped = "channel" in record or ("text" in record and "type" not in record)
    event = _object(record.get("text")) if wrapped else record
    if not event:
        # Provider stdout may contain a non-JSON diagnostic; no counters inferred.
        state.partial = True
        return
    _event(state, event)


def _consume(state: _State, data: bytes) -> None:
    if state.dropping:
        newline = data.find(b"\n")
        if newline < 0:
            return
        data = data[newline + 1:]
        state.dropping = False
    combined = state.pending + data
    state.pending = b""
    start = 0
    while True:
        newline = combined.find(b"\n", start)
        if newline < 0:
            break
        line = combined[start:newline]
        if len(line) > MAX_LINE_BYTES:
            state.partial = True
        else:
            _line(state, line)
        start = newline + 1
    remainder = combined[start:]
    if len(remainder) > MAX_LINE_BYTES:
        state.partial = True
        state.dropping = True
    else:
        state.pending = remainder


def _safe_path(root: Path, process_id: int) -> Path:
    project = root.resolve(strict=True)
    runtime = (project / ".ai" / "runtime").resolve(strict=True)
    runtime.relative_to(project)
    path = (runtime / f"process-{process_id}" / "events.jsonl").resolve(strict=True)
    path.relative_to(runtime)
    path.relative_to(project)
    return path


def _summary(state: _State) -> dict:
    if state.provider == "claude" and state.result is not None:
        values, source = state.result.copy(), "claude_result"
    else:
        rows = state.codex if state.provider == "codex" else state.claude
        source = "codex_turns" if state.provider == "codex" else "claude_messages"
        values = {
            name: (sum(row[name] for row in rows.values())
                   if rows and all(row[name] is not None for row in rows.values()) else None)
            for name in _FIELDS
        }
        if not rows:
            source = "unavailable"
    incomplete = state.partial or bool(state.pending) or state.dropping or state.offset < state.size
    complete = (state.terminal and not incomplete and
                values["input_tokens"] is not None and values["output_tokens"] is not None)
    return {**values, "source": source + (":partial" if incomplete else ""),
            "complete": complete}


def read_usage(root: Path, process_id: int) -> dict:
    """Return observed counters from a process log with bounded incremental I/O.

    A first read examines at most the last MAX_READ_BYTES, then follows appends.
    It never claims completeness after omitting the beginning. Identity changes,
    truncation, same-size rewrites and changed boundary bytes reset the cache.
    A burst larger than the read budget is consumed over successive refreshes.
    """
    if type(process_id) is not int or process_id <= 0:
        return _unknown("unavailable")
    with _LOCK:
        try:
            project = Path(root).resolve(strict=True)
            key = (str(project), process_id)
            path = _safe_path(project, process_id)
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return _unknown("unavailable")
                # Recheck containment and identity before reading opened bytes.
                checked = _safe_path(project, process_id)
                if checked != path or _identity(checked.stat()) != _identity(info):
                    _CACHE.pop(key, None)
                    return _unknown("unavailable")
                state = _CACHE.get(key)
                reset = (state is None or state.identity != _identity(info) or
                         info.st_size < state.offset or
                         (info.st_size == state.size and info.st_mtime_ns != state.mtime))
                if (state is not None and not reset and state.offset == info.st_size and
                        state.size == info.st_size and state.mtime == info.st_mtime_ns):
                    _CACHE.move_to_end(key)
                    return _summary(state)
                if state is not None and not reset and state.anchor:
                    stream.seek(state.offset - len(state.anchor))
                    reset = stream.read(len(state.anchor)) != state.anchor
                if reset:
                    state = _State(identity=_identity(info))
                    state.offset = max(0, info.st_size - MAX_READ_BYTES)
                    state.partial = state.offset > 0
                    state.dropping = state.offset > 0
                    if state.offset:
                        stream.seek(state.offset - 1)
                        state.dropping = stream.read(1) != b"\n"
                state = cast(_State, state)
                state.size = info.st_size
                state.mtime = info.st_mtime_ns
                stream.seek(state.offset)
                data = stream.read(min(MAX_READ_BYTES, max(0, state.size - state.offset)))
                state.offset += len(data)
                _consume(state, data)
                stream.seek(max(0, state.offset - 128))
                state.anchor = stream.read(min(128, state.offset))
                _CACHE[key] = state
                _CACHE.move_to_end(key)
                while len(_CACHE) > MAX_CACHE_FILES:
                    _CACHE.popitem(last=False)
                return _summary(state)
        except FileNotFoundError:
            if "key" in locals():
                _CACHE.pop(key, None)
            return _unknown("missing")
        except (OSError, ValueError, RuntimeError, TypeError):
            if "key" in locals():
                _CACHE.pop(key, None)
            return _unknown("unavailable")




def read_details(root: Path, process_id: int) -> dict:
    """Extra provenance without changing the existing token-reader contract."""
    usage = read_usage(root, process_id)
    with _LOCK:
        state = _CACHE.get((str(root.resolve()), process_id))
        return {**usage, "agent_turns": state.agent_turns if state else None,
                "counter_scope": "session_total" if state and state.result is not None else "observed_events",
                "stream_complete": bool(state and not state.partial and not state.pending
                                        and not state.dropping and state.offset == state.size)}


def scan_details(root: Path, process_id: int, *, max_bytes: int = 67_108_864) -> dict:
    """Host-only full-history scan for a durable receipt, not a dashboard poll.

    Reuses the same snapshot/turn deduplication as the live reader. Oversized
    logs remain partial rather than silently reporting their tail as a total.
    """
    try:
        path = _safe_path(root, process_id)
        state = _State()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return _unknown("unavailable")
            if path != _safe_path(root, process_id):
                return _unknown("unavailable")
            if _identity(path.stat()) != _identity(before):
                return _unknown("unavailable")
            state.size = before.st_size
            while state.offset < min(state.size, max_bytes):
                data = stream.read(min(MAX_READ_BYTES, max_bytes - state.offset))
                if not data:
                    break
                state.offset += len(data)
                _consume(state, data)
            after = os.fstat(stream.fileno())
            state.partial |= (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        return {**_summary(state), "agent_turns": state.agent_turns,
                "counter_scope": "session_total" if state.result is not None else "observed_events",
                "stream_complete": not state.partial and not state.pending and state.offset == state.size}
    except (OSError, ValueError, RuntimeError, TypeError):
        return _unknown("unavailable")
