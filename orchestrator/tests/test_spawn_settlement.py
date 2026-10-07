"""Concurrent reconciliation must not fence a fresh launch before its PID is saved."""
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, reconcile


def test_fresh_current_generation_claim_keeps_lease_without_inventing_liveness(conn):
    task = db.create_task(conn, title='starting', status='LEASED', owned_paths=['new.py'])
    db.bump_generation(conn, task)
    db.open_worker_run(conn, task, 1, 'codex')
    db.acquire_leases(conn, task, ['new.py'], generation=1)
    report = reconcile.ReconcileReport()
    reconcile._detect_stale_workers(conn, report, adopt_running=True)
    assert db.get_task(conn, task)['status'] == 'LEASED'
    assert db.active_leases(conn)
    assert 'PID not yet recorded' in report.notes[0]


@pytest.mark.parametrize('problem', ['old', 'ended', 'wrong_generation'])
def test_missing_pid_claim_is_not_protected_forever_or_after_exit(conn, problem):
    task = db.create_task(conn, title='crashed', status='LEASED', owned_paths=['new.py'])
    db.bump_generation(conn, task)
    run = db.open_worker_run(conn, task, 1, 'codex')
    db.acquire_leases(conn, task, ['new.py'], generation=1)
    if problem == 'old':
        stamp = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
        conn.execute('UPDATE worker_runs SET started_at=? WHERE id=?', (stamp, run))
    elif problem == 'ended':
        db.close_worker_run(conn, run, 1)
    else:
        db.bump_generation(conn, task)
    reconcile._detect_stale_workers(conn, reconcile.ReconcileReport(), adopt_running=True)
    assert db.get_task(conn, task)['status'] == 'STALE'
    assert not db.active_leases(conn)
