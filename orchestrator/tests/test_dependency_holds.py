"""Dependency completion must not clear a concrete manager/worker hold."""
import pytest

from agentkit import db


@pytest.mark.parametrize("reason", [
    "[AgentKit execution limit] max_turns reached",
    "Independent tester found a source defect",
    "User decision required",
])
def test_concrete_blocker_survives_repeated_dependency_refresh(conn, reason):
    held = db.create_task(conn, title="Held", status="BLOCKED")
    planned = db.create_task(conn, title="Fresh", status="PLANNED")
    db.update_task(conn, held, blocker=reason)
    assert db.refresh_ready(conn) == [planned]
    assert db.refresh_ready(conn) == []
    assert db.get_task(conn, held)["status"] == "BLOCKED"
    assert db.get_task(conn, held)["blocker"] == reason


def test_unknown_blocker_metadata_fails_closed(conn):
    task = db.create_task(conn, title="Unknown hold", status="BLOCKED")
    db.update_task(conn, task, blocked_meta="not-json")
    assert db.refresh_ready(conn) == []
    assert db.get_task(conn, task)["status"] == "BLOCKED"


def test_blocked_without_separate_reason_stays_held(conn):
    task = db.create_task(conn, title="Worker-reported hold", status="BLOCKED")
    assert db.refresh_ready(conn) == []
    assert db.refresh_ready(conn) == []
    assert db.get_task(conn, task)["status"] == "BLOCKED"
