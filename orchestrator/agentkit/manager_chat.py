"""The manager as a chat in the AgentKit view (#4).

After a usage-limit reset the supervisor's coordinator, a resumed Claude or Codex
session, continues the work. The user reads what it says and writes to it in the
view, as in a native chat. One conversation per job:
- user messages, saved here and delivered once;
- manager replies, read from coordinator session events (assistant text and the
  final result) or written by the external manager chat with manager_say.

A pending message wakes the coordinator when no external manager is attached
(the supervisor checks), and is placed in its prompt; an attached external chat
reads it with manager_messages instead, so there is always one manager.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .locking import atomic_write, exclusive

LIMIT = 4000


def _path(root, job_id: str) -> Path:
    from .jobs import path as job_path
    job_path(root, job_id)  # validates the job id as a slug
    return Path(root) / ".ai" / "runtime" / "manager-chat" / f"{job_id}.json"


def _load(root, job_id: str) -> list[dict]:
    try:
        data = json.loads(_path(root, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _append(root, job_id: str, entry: dict) -> dict:
    target = _path(root, job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(root, "manager-chat"):
        entries = _load(root, job_id)
        entry = {"id": len(entries) + 1, "at": db.utcnow(), **entry}
        atomic_write(target, json.dumps([*entries, entry], indent=1) + "\n")
    return entry


def _text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("a chat message needs text")
    if len(text) > LIMIT:
        raise ValueError(f"a chat message is at most {LIMIT} characters")
    return text


def post(root, job_id: str, text: str) -> dict:
    """A user message from the view; pending until a manager receives it."""
    return _append(root, job_id, {"from": "user", "text": _text(text), "delivered_to": None})


def say(root, job_id: str, text: str) -> dict:
    """A reply from the external manager chat, shown in the view."""
    return _append(root, job_id, {"from": "manager", "text": _text(text), "source": "external manager"})


def pending(root, job_id: str) -> list[dict]:
    return [e for e in _load(root, job_id) if e.get("from") == "user" and not e.get("delivered_to")]


def mark_delivered(root, job_id: str, ids: list[int], *, to: str) -> None:
    with exclusive(root, "manager-chat"):
        entries = _load(root, job_id)
        for entry in entries:
            if entry.get("id") in ids and not entry.get("delivered_to"):
                entry.update(delivered_to=to, delivered_at=db.utcnow())
        atomic_write(_path(root, job_id), json.dumps(entries, indent=1) + "\n")


def prompt_section(root, job_id: str) -> str:
    """Pending user messages for the coordinator prompt, and how to read new ones while it works."""
    live = (f"\nThe user can write to you in the AgentKit view chat while you work: call "
            f"manager_messages(job_id='{job_id}') between major steps and act on what it returns. Your text "
            "replies are shown to the user in the view automatically (do not call manager_say): answer "
            "plainly, as in a chat, and say what you did.")
    waiting = pending(root, job_id)
    if not waiting:
        return live
    lines = "\n".join(f"- [{m['at']}] {m['text']}" for m in waiting)
    return "\nThe user wrote to you in the AgentKit view chat (newest last):\n" + lines + live


def _replies(line: str) -> list[str]:
    """Assistant text in one stored stdout event line (Claude stream-json or Codex --json)."""
    try:
        event = json.loads(line)
    except ValueError:
        return []
    if not isinstance(event, dict):
        return []
    if event.get("type") == "assistant":
        blocks = (event.get("message") or {}).get("content") or []
        return [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    if event.get("type") == "result" and isinstance(event.get("result"), str) and event["result"].strip():
        return [event["result"]]
    item = event.get("item") if event.get("type") == "item.completed" else None
    if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
        return [item["text"]]
    return []


def _coordinator_replies(root, job_id: str) -> list[dict]:
    replies: list[dict] = []
    conn = db.connect_readonly(Path(root))
    try:
        rows = conn.execute("SELECT id FROM processes WHERE purpose='coordinator' AND job_id=? ORDER BY id",
                            (job_id,)).fetchall()
    finally:
        conn.close()
    for (identifier,) in rows:
        events = Path(root) / ".ai" / "runtime" / f"process-{identifier}" / "events.jsonl"
        try:
            lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("channel") != "stdout":
                continue
            for text in _replies(str(record.get("text", ""))):
                if not replies or replies[-1]["text"] != text:  # the final result repeats the last text
                    replies.append({"from": "manager", "text": text, "at": record.get("at", ""),
                                    "source": f"coordinator process {identifier}"})
    return replies


def transcript(root, job_id: str) -> list[dict]:
    """The whole conversation, oldest first."""
    entries = _load(root, job_id) + _coordinator_replies(root, job_id)
    return sorted(entries, key=lambda e: e.get("at") or "")


def jobs_with_chat(root) -> list[str]:
    """Every job id, for the view's chat selector."""
    folder = Path(root) / ".ai" / "jobs"
    return sorted(path.stem for path in folder.glob("*.json")) if folder.is_dir() else []
