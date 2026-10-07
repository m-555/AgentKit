"""PID reuse must not block migration or turn Chrome into a live worker."""
from __future__ import annotations

import json

from agentkit import process_identity as identity


def test_stored_birth_token_distinguishes_reused_pid(monkeypatch):
    monkeypatch.setattr(identity, "fingerprint", lambda p: {"kind":"windows","created":22})
    record = {"pid":123,"pid_identity":json.dumps({"kind":"windows","created":11})}
    assert not identity.alive(record, probe=lambda p: True)


def test_legacy_terminal_record_ignores_process_born_after_exit(monkeypatch):
    monkeypatch.setattr(identity, "fingerprint", lambda p: {"born":"2026-10-04T09:10:29+00:00"})
    record={"pid":123,"status":"FINISHED","ended_at":"2026-10-04T00:45:46+00:00"}
    assert not identity.alive(record, probe=lambda p: True)


def test_unknown_identity_remains_conservative(monkeypatch):
    monkeypatch.setattr(identity, "fingerprint", lambda p: None)
    assert identity.alive({"pid":123,"status":"FINISHED"}, probe=lambda p: True)
    assert not identity.alive({"pid":123}, probe=lambda p: False)


def test_live_monitor_birth_token_is_saved(conn, monkeypatch):
    from agentkit import processes
    identifier = conn.execute("INSERT INTO processes(purpose,provider,launch_json,started_at) VALUES('worker','codex','{}','now')").lastrowid
    monkeypatch.setattr(identity, "fingerprint", lambda p: {"created":123})
    processes.update(conn, identifier, pid=999)
    assert json.loads(processes.get(conn, identifier)["pid_identity"]) == {"created":123}


def test_protected_reused_pid_birth_does_not_hold_old_worker(monkeypatch):
    monkeypatch.setattr(identity, "fingerprint", lambda p: {"kind": "windows", "created": 220,
                                                          "source": "cim"})
    record = {"pid": 123, "pid_identity": json.dumps({"kind": "windows", "created": 110})}
    assert not identity.alive(record, probe=lambda p: True)
    record["pid_identity"] = json.dumps({"kind": "windows", "created": 221})
    assert identity.alive(record, probe=lambda p: True)


def test_cim_fallback_is_bounded_read_only_and_unknown_stays_unknown(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="134356139833347120\n")
    monkeypatch.setattr(identity.subprocess, "run", run)
    token = identity.windows_birth(123)
    assert token["created"] == 134356139833347120
    assert token["source"] == "cim"
    assert calls[0][1]["timeout"] == 5
    assert "Get-CimInstance" in calls[0][0][-1]
    assert identity.windows_birth("123; Stop-Process") is None
    monkeypatch.setattr(identity.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    assert identity.windows_birth(123) is None
