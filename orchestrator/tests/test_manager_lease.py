"""External manager ownership survives restarts and never duplicates control."""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentkit import (
    db,
    jobs,
    manager,
    manager_audit,
    manager_state,
    models,
    processes,
    supervisor,
)
from agentkit.adapters.base import Launch

PIN = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1)


@pytest.fixture
def external(project_root, conn):
    job = jobs.create(project_root, "external", "Continue authorized work", "codex")
    jobs.pin_coordinator(project_root, job["id"], PIN)
    return job


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_live_external_manager_fences_autonomous_coordinator(project_root, conn, project, external, monkeypatch):
    token = manager.attach(conn, project_root, external["id"], "native-manager", os.getpid())
    assert manager.blocks_spawn(conn, external["id"])
    monkeypatch.setattr(processes, "start", lambda *args, **kwargs: pytest.fail("competing coordinator"))
    supervisor._control_launch(conn, project, jobs.load(project_root, external["id"]), "coordinator")
    manager.heartbeat(conn, external["id"], token)
    identifier = manager.checkpoint(conn, external["id"], token, {"decisions": ["keep Sol"], "tests": ["focused passed"], "next_action": "audit integration"})
    assert identifier and manager.packet(conn, external["id"])["external_checkpoint"]["next_action"] == "audit integration"
    with pytest.raises(PermissionError):
        manager.heartbeat(conn, external["id"], "wrong-token")


def test_released_or_expired_lease_requires_confirmed_bridge_death(project_root, conn, external):
    token = manager.attach(conn, project_root, external["id"], "native", os.getpid())
    manager.release(conn, external["id"], token)
    assert manager.blocks_spawn(conn, external["id"])
    conn.execute("UPDATE manager_leases SET pid=? WHERE job_id=?", (dead_pid(), external["id"]))
    assert not manager.blocks_spawn(conn, external["id"])
    assert manager_state.pending(conn, external["id"])
    assert manager_state.state(conn, external["id"])["epoch"] == 1
    assert not manager.blocks_spawn(conn, external["id"])
    assert manager_state.state(conn, external["id"])["epoch"] == 1


def test_expired_live_holder_can_renew_but_must_audit(project_root, conn, external):
    token = manager.attach(conn, project_root, external["id"], "native", os.getpid())
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?", (past, external["id"]))
    assert manager.blocks_spawn(conn, external["id"])
    manager.heartbeat(conn, external["id"], token)
    assert manager.fresh(manager.lease(conn, external["id"])) and manager_state.pending(conn, external["id"])
    report = manager_audit.run(conn, project_root, external["id"], token=token)
    manager_audit.acknowledge(conn, project_root, external["id"], report["epoch"], report["digest"], "Inspected durable memory after heartbeat loss", token=token)
    assert not manager_state.pending(conn, external["id"])


@pytest.mark.parametrize("status", ["FAILED", "FINISHED"])
def test_terminal_control_row_with_live_child_blocks_attach_and_spawn(project_root, conn, project, external, monkeypatch, status):
    conn.execute("INSERT INTO processes(purpose,provider,job_id,status,pid,child_pid,launch_json,started_at) VALUES('coordinator','codex',?,?,?,?,'{}',?)", (external["id"], status, dead_pid(), os.getpid(), db.utcnow()))
    with pytest.raises(ValueError, match="already owns"):
        manager.attach(conn, project_root, external["id"], "native", os.getpid())
    monkeypatch.setattr(processes, "start", lambda *args, **kwargs: pytest.fail("duplicate spawn"))
    supervisor._control_launch(conn, project, jobs.load(project_root, external["id"]), "coordinator")


def test_dead_external_bridge_recovers_saved_cli_session_not_native_reference(project_root, conn, project, external, monkeypatch):
    token = manager.attach(conn, project_root, external["id"], "native", os.getpid(), session_ref="native-chat-reference")
    manager.checkpoint(conn, external["id"], token, {"next_action": "inspect saved changes"})
    manager.release(conn, external["id"], token)
    conn.execute("UPDATE manager_leases SET pid=? WHERE job_id=?", (dead_pid(), external["id"]))
    conn.execute("UPDATE jobs SET coordinator_session='saved-codex-cli' WHERE id=?", (external["id"],))
    from agentkit import adapters
    adapter = adapters.get("codex")
    monkeypatch.setattr(adapter, "detect", lambda: SimpleNamespace(version="test", path="fake-codex"))
    launches = []
    monkeypatch.setattr(processes, "start", lambda connection, root, launch, **kwargs: launches.append(launch) or 99)
    supervisor._control_launch(conn, project, jobs.load(project_root, external["id"]), "coordinator")
    assert "saved-codex-cli" in launches[0].argv and "native-chat-reference" not in launches[0].argv
    assert "manager_audit" in launches[0].stdin_text and "inspect saved changes" in launches[0].stdin_text


def test_concurrent_stale_attachments_claim_exactly_one_manager(project_root, conn, external):
    manager.attach(conn, project_root, external["id"], "old", os.getpid())
    conn.execute("UPDATE manager_leases SET pid=?,heartbeat_at=? WHERE job_id=?", (dead_pid(), "2000-01-01T00:00:00+00:00", external["id"]))
    def attach(holder):
        connection = db.connect(project_root)
        try:
            return manager.attach(connection, project_root, external["id"], holder, os.getpid())
        except ValueError:
            return None
        finally:
            connection.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attach, ["first", "second"]))
    assert sum(value is not None for value in results) == 1


def test_external_attach_and_control_start_share_atomic_claim(project_root, conn, external, monkeypatch):
    monkeypatch.setattr(processes.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=os.getpid()))
    def claim(kind):
        connection = db.connect(project_root)
        try:
            if kind == "external":
                return manager.attach(connection, project_root, external["id"], "native", os.getpid())
            return processes.start(connection, project_root, Launch(["never-executed"]), purpose="coordinator", provider="codex", job_id=external["id"])
        except ValueError:
            return None
        finally:
            connection.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ["external", "process"]))
    assert sum(value is not None for value in results) == 1


def test_checkpoint_mirror_tracks_committed_database_memory(project_root, conn, external):
    import json
    token = manager.attach(conn, project_root, external["id"], "native", os.getpid())
    for action in ("first", "updated"):
        identifier = manager.checkpoint(conn, external["id"], token, {"next_action": action})
        for mirror in (project_root / ".ai/runtime/manager-checkpoint.json",
                       project_root / ".ai/runtime/manager-checkpoints" / (external["id"] + ".json")):
            value = json.loads(mirror.read_text())
            assert value["checkpoint_id"] == identifier and value["next_action"] == action


def test_failed_checkpoint_mirror_does_not_lose_database_memory(project_root, conn, external, monkeypatch):
    from agentkit import manager_mirror
    token = manager.attach(conn, project_root, external["id"], "native", os.getpid())
    def fail(*args):
        raise PermissionError("read-only mirror")
    monkeypatch.setattr(manager_mirror, "atomic_write", fail)
    identifier = manager.checkpoint(conn, external["id"], token, {"next_action": "preserved"})
    assert identifier and manager.packet(conn, external["id"])["external_checkpoint"]["next_action"] == "preserved"
    assert db.recent_events(conn, kind="manager_checkpoint_mirror_failed")
