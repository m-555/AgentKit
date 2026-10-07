"""Bounded public messages and tool labels, shared by CLI viewers and the dashboard."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

from .live import _activity
from .secrets import redact_text
from .usage_receipts import path_for

MAX_BYTES = 262_144
MAX_MESSAGES = 60
MAX_TEXT = 12_000


def _object(value) -> dict:
    try:
        value = json.loads(value) if isinstance(value, (str, bytes)) else value
        return value if isinstance(value, dict) else {}
    except (ValueError, RecursionError):
        return {}


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Redact the whole field before truncating; preserve ordinary line breaks.
    return "".join(c for c in redact_text(value) if c.isprintable() or c in "\n\t")[:MAX_TEXT]


def messages(event: dict) -> list[dict]:
    kind = event.get("type")
    output = []
    if kind == "assistant":
        message = _object(event.get("message"))
        content = message.get("content")
        if isinstance(content, list):
            for block in content[:100]:
                if isinstance(block, dict) and block.get("type") == "text":
                    value = _clean(block.get("text"))
                    if value:
                        output.append({"kind": "message", "text": value,
                                       "message_id": str(message.get("id") or "")[:256]})
    elif kind == "item.completed":
        item = _object(event.get("item"))
        if item.get("type") == "agent_message":
            value = _clean(item.get("text"))
            if value:
                output.append({"kind": "message", "text": value,
                               "message_id": str(item.get("id") or "")[:256]})
    elif kind == "result":
        value = _clean(event.get("result"))
        if value:
            output.append({"kind": "message", "text": value, "message_id": "result"})
    for activity in _activity(event):
        label = " ".join(str(activity.get(key) or "") for key in ("kind", "name", "state")).strip()
        output.append({"kind": "activity", "text": _clean(label), "message_id": ""})
    return output


def read(root: Path, identifier: int) -> dict:
    result: dict = {"status": "missing", "messages": [], "partial": False, "cursor": None}
    try:
        path = path_for(root, identifier, "events.jsonl")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or path != path_for(root, identifier, "events.jsonl"):
                return result
            checked = path.stat()
            if (checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino):
                return result
            offset = max(0, info.st_size - MAX_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_BYTES)
        lines = data.splitlines(keepends=True)
        if offset:
            offset += len(lines.pop(0)) if lines else 0
        seen = set()
        for line in lines:
            offset += len(line)
            if not line.endswith(b"\n"):
                result["partial"] = True
                continue
            record = _object(line)
            if record.get("channel") == "stderr":
                continue
            event = _object(record.get("text")) if "channel" in record else record
            for index, message in enumerate(messages(event)):
                key = (message["message_id"], message["text"])
                if message["kind"] == "message" and key in seen:
                    continue
                seen.add(key)
                result["messages"].append({**message, "at": _clean(record.get("at")),
                                           "id": f"{info.st_dev}:{info.st_ino}:{offset}:{index}"})
        result["partial"] |= info.st_size > MAX_BYTES or len(result["messages"]) > MAX_MESSAGES
        result["messages"] = result["messages"][-MAX_MESSAGES:]
        result.update(status="ok", cursor=f"{info.st_dev}:{info.st_ino}:{info.st_size}")
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return result


def endpoint(root: Path, identifier: int) -> tuple[int, dict]:
    """Only recorded numeric process IDs; never accept a filename from HTTP."""
    from . import db
    try:
        conn = db.connect_readonly(root)
        try:
            exists = conn.execute("SELECT 1 FROM processes WHERE id=?", (identifier,)).fetchone()
        finally:
            conn.close()
        if not exists:
            return 404, {"status": "missing", "messages": []}
        return 200, read(root, identifier)
    except (OSError, ValueError, sqlite3.Error):
        return 503, {"status": "unavailable", "messages": []}
