"""F31: a changed agent CLI or guard code is re-proven by the supervisor, not waited on.

After VS Code restarted, every Claude task sat READY with "claude-code 2.1.286
requires a fresh functional confinement probe": AgentKit's guard digest had
changed, and only a manual `agentkit probe --functional` could clear it. The
supervisor now starts that probe for the one adapter, once per identity per hour.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from agentkit import auto_probe, capabilities, db

BLOCKER = "claude-code 2.1.286 requires a fresh functional confinement probe; cached runtime version differs"


def _waiting(conn):
    task = db.create_task(conn, title="builder", status="READY", owned_paths=["a.py"], expected_write=["a.py"])
    db.update_task(conn, task, blocker=BLOCKER)
    return task


def test_a_task_waiting_for_a_probe_starts_one_probe_per_identity(project, conn, monkeypatch):
    _waiting(conn)
    monkeypatch.setattr(auto_probe, "_identity", lambda project, name: "identity-1")
    started = []
    launch = lambda root, name: started.append(name)  # noqa: E731
    assert auto_probe.tick(conn, project, launch=launch)
    assert started == ["claude-code"]
    assert auto_probe.tick(conn, project, launch=launch) == []  # not again within the hour
    assert db.recent_events(conn, kind="auto_probe_started")


def test_a_failed_probe_is_retried_after_an_hour_or_for_a_new_identity(project, conn, monkeypatch):
    _waiting(conn)
    identity = {"value": "identity-1"}
    monkeypatch.setattr(auto_probe, "_identity", lambda project, name: identity["value"])
    started = []
    launch = lambda root, name: started.append(name)  # noqa: E731
    first = db.parse_ts(db.utcnow())
    auto_probe.tick(conn, project, launch=launch, now=first)
    assert auto_probe.tick(conn, project, launch=launch, now=first + timedelta(minutes=30)) == []
    assert auto_probe.tick(conn, project, launch=launch, now=first + timedelta(minutes=61))
    identity["value"] = "identity-2"  # Claude Code updated again
    assert auto_probe.tick(conn, project, launch=launch, now=first + timedelta(minutes=62))
    assert started == ["claude-code"] * 3


def test_no_waiting_task_means_no_probe(project, conn, monkeypatch):
    task = db.create_task(conn, title="builder", status="READY")
    db.update_task(conn, task, blocker="selected model or account is unavailable")
    assert auto_probe.tick(conn, project, launch=lambda *a: (_ for _ in ()).throw(AssertionError("probe"))) == []


def test_the_probe_process_saves_the_measured_capabilities(project_root, monkeypatch):
    adapter = SimpleNamespace(name="claude-code")
    install = SimpleNamespace(name="claude-code", version="2.1.286")
    monkeypatch.setattr("agentkit.adapters.installed", lambda project: [(adapter, install)])
    monkeypatch.setattr("agentkit.probe.probe_static",
                        lambda adapter, install: capabilities.CapabilitySet(adapter="claude-code", version="2.1.286"))

    def functional(adapter, install, caps, fixture_root):
        caps.set("prewrite_file_guard", True, "functional v2: attempted violating tool was guard-blocked")
        return caps

    result = auto_probe.probe_one(project_root, "claude-code", functional=functional)
    assert result["capabilities"]["prewrite_file_guard"] is True
    saved = json.loads(capabilities.cache_path(project_root).read_text())
    assert saved["adapters"]["claude-code"]["capabilities"]["prewrite_file_guard"] is True
