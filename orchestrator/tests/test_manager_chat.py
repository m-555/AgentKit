"""#4: the manager is a chat in the AgentKit view.

After a usage-limit reset the supervisor's coordinator, a resumed Claude session,
continues the work. The user wants to read what it says and write to it, as in
the VS Code chat. The view shows one conversation per job: the user's messages
and the manager's replies, from the coordinator's own session events or from
the external manager chat (manager_say). A pending user message wakes the
coordinator when no external manager is attached; the attached chat reads it
with manager_messages, so there is always one manager.
"""
from __future__ import annotations

import json

import pytest

from agentkit import db, manager_chat

JOB = "chat-job"


def _event(path, inner, channel="stdout"):
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": "2026-10-10T01:00:00+00:00", "channel": channel,
                              "text": json.dumps(inner) if isinstance(inner, dict) else inner}) + "\n")


@pytest.fixture
def coordinator_run(project_root, conn):
    process = conn.execute("INSERT INTO processes(purpose,provider,job_id,status,launch_json,started_at) "
                           "VALUES('coordinator','claude-code',?,'FINISHED','{}','2026-10-10T00:42:32+00:00')",
                           (JOB,)).lastrowid
    events = project_root / ".ai" / "runtime" / f"process-{process}" / "events.jsonl"
    events.parent.mkdir(parents=True)
    _event(events, "Ignoring permissions entries", channel="stderr")
    _event(events, {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Epoch 48 audit passed; acknowledging."}, {"type": "tool_use", "name": "x"}]}})
    _event(events, {"type": "result", "result": "Done: task 142 relaunches."})
    return process


def test_user_messages_are_saved_and_pending_until_delivered(project_root):
    first = manager_chat.post(project_root, JOB, "Please prioritise the selection route.")
    assert [m["id"] for m in manager_chat.pending(project_root, JOB)] == [first["id"]]
    manager_chat.mark_delivered(project_root, JOB, [first["id"]], to="coordinator process 9")
    assert manager_chat.pending(project_root, JOB) == []


@pytest.mark.parametrize("text", ["", "   ", "x" * (manager_chat.LIMIT + 1)])
def test_empty_or_oversized_messages_are_refused(project_root, text):
    with pytest.raises(ValueError):
        manager_chat.post(project_root, JOB, text)


def test_the_transcript_merges_user_messages_and_coordinator_replies(project_root, conn, coordinator_run):
    manager_chat.post(project_root, JOB, "How did the reset go?")
    manager_chat.say(project_root, JOB, "I am the VS Code manager; the coordinator handled it.")
    entries = manager_chat.transcript(project_root, JOB)
    texts = [(e["from"], e["text"]) for e in entries]
    assert ("manager", "Epoch 48 audit passed; acknowledging.") in texts
    assert ("manager", "Done: task 142 relaunches.") in texts
    assert ("user", "How did the reset go?") in texts
    assert ("manager", "I am the VS Code manager; the coordinator handled it.") in texts
    assert not any("permissions entries" in t for _, t in texts)  # stderr is not conversation
    assert all(e["at"] for e in entries) and entries == sorted(entries, key=lambda e: e["at"])


def test_codex_agent_messages_are_replies_too(project_root, conn):
    process = conn.execute("INSERT INTO processes(purpose,provider,job_id,status,launch_json,started_at) "
                           "VALUES('coordinator','codex',?,'FINISHED','{}',?)", (JOB, db.utcnow())).lastrowid
    events = project_root / ".ai" / "runtime" / f"process-{process}" / "events.jsonl"
    events.parent.mkdir(parents=True)
    _event(events, {"type": "item.completed", "item": {"type": "agent_message", "text": "Planned three tasks."}})
    assert ("manager", "Planned three tasks.") in [(e["from"], e["text"]) for e in manager_chat.transcript(project_root, JOB)]


def test_the_coordinator_prompt_carries_pending_messages(project_root):
    manager_chat.post(project_root, JOB, "Stop after the route task.")
    section = manager_chat.prompt_section(project_root, JOB)
    assert "Stop after the route task." in section and "view" in section
    quiet = manager_chat.prompt_section(project_root, "other-job")
    assert "Stop after the route task." not in quiet and "manager_messages" in quiet


def test_the_view_serves_the_conversation_and_the_job_list(project_root, coordinator_run):
    from agentkit import jobs
    from tests.test_dashboard import request, serving
    jobs.create(project_root, JOB, "Build it", "claude-code")
    manager_chat.post(project_root, JOB, "Hello manager")
    with serving(project_root) as server:
        status, _, body = request(server, f"/api/manager-chat?job={JOB}")
        assert status == 200
        texts = [m["text"] for m in json.loads(body)["messages"]]
        assert "Hello manager" in texts and "Epoch 48 audit passed; acknowledging." in texts
        status, _, body = request(server, "/api/manager-chat")
        assert JOB in json.loads(body)["jobs"]
        status, _, page = request(server, "/")
        assert b'id="manager-chat"' in page and b"/dashboard_chat.js" in page


def test_sending_from_the_view_needs_the_operator_token(project_root):
    import io

    from agentkit.dashboard_actions import Controls
    controls = Controls(project_root, True)
    body = json.dumps({"job_id": JOB, "text": "Go ahead"}).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    assert controls.post("/api/manager-chat", headers, io.BytesIO(body))[0] == 403
    status, _ = controls.post("/api/manager-chat", {**headers, "X-AgentKit-Token": controls.token}, io.BytesIO(body))
    assert status == 200 and manager_chat.pending(project_root, JOB)[0]["text"] == "Go ahead"


def test_a_coordinators_reply_is_not_saved_twice(project_root, conn, monkeypatch):
    """Its own text already shows in the view; saving it again showed it twice as 'VS Code chat'."""
    from agentkit import jobs, mcp_manager
    jobs.create(project_root, JOB, "Build it", "claude-code")
    process = conn.execute("INSERT INTO processes(purpose,provider,job_id,status,launch_json,started_at) "
                           "VALUES('coordinator','claude-code',?,'RUNNING','{}',?)", (JOB, db.utcnow())).lastrowid
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setenv("AGENTKIT_PROCESS", str(process))
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    assert json.loads(mcp_manager.manager_say(JOB, "Recovery acknowledged."))["saved"] is False
    assert not [e for e in manager_chat.transcript(project_root, JOB) if e.get("source") == "external manager"]


def test_a_worker_cannot_read_or_answer_the_chat(project_root, monkeypatch):
    from agentkit import mcp_manager
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.setenv("AGENTKIT_TASK", "3")
    manager_chat.post(project_root, JOB, "Secret plan")
    for call in (lambda: mcp_manager.manager_messages(JOB), lambda: mcp_manager.manager_say(JOB, "hi")):
        with pytest.raises(PermissionError):
            call()
    assert manager_chat.pending(project_root, JOB)  # still waiting for the manager
