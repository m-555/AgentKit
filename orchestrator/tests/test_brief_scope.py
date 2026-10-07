"""A session launched for one task reads only that task's brief.

A local model once called `brief(task_id=1)` with an id it invented, received
another task's assignment, and read that task's files before finding its own.
A session with an assigned task is now told its own task instead.
"""
import pytest

from agentkit import db, mcp_server


@pytest.fixture
def two_tasks(project_root, conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "_root", lambda: project_root)
    for name in ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_ROLE"):
        monkeypatch.delenv(name, raising=False)
    own = db.create_task(conn, title="Own assignment", owned_paths=["services/retry.py"], status="RUNNING")
    other = db.create_task(conn, title="Unrelated assignment", owned_paths=["services/media.py"], status="RUNNING")
    return own, other


def test_a_worker_is_refused_another_tasks_brief_and_told_its_own(two_tasks, monkeypatch):
    own, other = two_tasks
    monkeypatch.setenv("AGENTKIT_TASK", str(own))
    text = mcp_server.brief(other)
    assert "Unrelated assignment" not in text
    assert f"task {own}" in text and "no task_id" in text


def test_a_worker_still_reads_its_own_brief(two_tasks, monkeypatch):
    own, _other = two_tasks
    monkeypatch.setenv("AGENTKIT_TASK", str(own))
    assert "Own assignment" in mcp_server.brief()
    assert "Own assignment" in mcp_server.brief(own)


def test_an_unassigned_manager_session_reads_any_brief(two_tasks):
    _own, other = two_tasks
    assert "Unrelated assignment" in mcp_server.brief(other)
