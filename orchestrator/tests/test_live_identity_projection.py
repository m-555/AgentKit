"""The dashboard must not treat a reused PID as the previous agent."""
import json

from agentkit import live, process_identity


def test_snapshot_checks_saved_birth_identity_before_reporting_liveness(project_root, conn, monkeypatch):
    identifier = conn.execute(
        'INSERT INTO processes(purpose,provider,status,pid,child_pid,pid_identity,child_pid_identity,started_at,ended_at,launch_json) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        ('worker', 'codex', 'FINISHED', 9001, 9002,
         json.dumps({'kind': 'windows', 'created': 1}),
         json.dumps({'kind': 'windows', 'created': 2}),
         '2026-10-01T00:00:00+00:00', '2026-10-01T01:00:00+00:00', '{}')).lastrowid
    monkeypatch.setattr(live, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(process_identity, 'fingerprint',
                        lambda pid: {'kind': 'windows', 'created': 999})
    row = live.snapshot(project_root, process_id=identifier)['processes'][0]
    assert row['monitor_alive'] is False and row['child_alive'] is False
    assert row['state'] == 'STOPPED' and row['liveness_risk'] is False
    assert 'pid_identity' not in row and 'child_pid_identity' not in row


def test_current_birth_identity_remains_live(project_root, conn, monkeypatch):
    saved = {'kind': 'windows', 'created': 999}
    identifier = conn.execute(
        'INSERT INTO processes(purpose,provider,status,pid,pid_identity,started_at,launch_json) VALUES(?,?,?,?,?,?,?)',
        ('worker', 'codex', 'RUNNING', 9001, json.dumps(saved),
         '2026-10-01T00:00:00+00:00', '{}')).lastrowid
    monkeypatch.setattr(live, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(process_identity, 'fingerprint', lambda pid: saved)
    row = live.snapshot(project_root, process_id=identifier)['processes'][0]
    assert row['monitor_alive'] is True and row['state'] == 'RUNNING'
