"""An agent's window reads like a chat.

The user opened the coordinator's window after a reset and could not follow it:
it repainted a thirty-line status frame (processes, quota, tasks) on every tool
call, so the manager's own words scrolled away between frames, among "quota"
lines. The window now prints a header once, what the agent says, one short line
per tool, and a status line only when the status changes.
"""
from __future__ import annotations

import json

from agentkit import windows


def _write(root, identifier, events):
    folder = root / ".ai" / "runtime" / f"process-{identifier}"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "events.jsonl").open("w", encoding="utf-8") as out:
        for event in events:
            out.write(json.dumps({"at": "2026-10-10T13:03:35+00:00", "channel": "stdout",
                                  "text": json.dumps(event)}) + "\n")


def _view(state="RUNNING"):
    process = {"id": 197, "role": "coordinator", "model": "claude-opus-5-5", "effort": "high",
               "status": state, "state": state, "task_id": None, "job_id": "pilot",
               "monitor_alive": True, "child_alive": True}
    return {"status": "ok", "processes": [process], "tasks": [], "external_managers": []}


def test_the_manager_window_shows_its_words_tools_and_status_changes(tmp_path):
    _write(tmp_path, 197, [
        {"type": "system", "subtype": "init"},
        {"type": "rate_limit_event"},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "name": "mcp__agentkit__manager_audit"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "text", "text": "Recovery audit acknowledged.\nNow checking the job criteria."}]}},
    ])
    snapshots = iter([_view(), _view(), _view("FINISHING"), {**_view(), "processes": []}])
    shown = []
    assert windows.watch(tmp_path, 197, reader=lambda *args: next(snapshots), sleeper=lambda s: None,
                         output=shown.append) == 0
    text = "\n".join(shown)
    assert shown[0].startswith("AgentKit #197 · Manager · claude-opus-5-5 (effort high) · job pilot")
    assert text.count("AgentKit #197") == 1  # the header is never repainted
    assert "· uses manager_audit" in text
    assert "Manager:\n  Recovery audit acknowledged.\n  Now checking the job criteria." in text
    assert text.count("Recovery audit acknowledged.") == 1
    assert "quota" not in text and "PROVIDER QUOTA" not in text
    assert [line.split("status: ")[1] for line in shown if "status: " in line] == ["RUNNING", "FINISHING"]


def test_speakers_use_the_users_words():
    assert windows.speaker("coordinator") == "Manager"
    assert windows.speaker("backend-builder") == "Builder"
    assert windows.speaker("frontend-tester") == "Tester"
