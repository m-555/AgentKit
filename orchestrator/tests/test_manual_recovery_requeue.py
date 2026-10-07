"""Fresh authorized retries retire old recovery claims without erasing proofs."""
import pytest

from agentkit import db, mcp_server, processes, recovery_runtime
from agentkit import recovery_store as store


def parked(conn):
    task = db.create_task(conn, title='local assessment', status='READY')
    session = store.register(conn, identifier='local-test', provider='local-opencode', account='default',
        host='cli', role='worker', reference='old', policy='unmetered', task_id=task)
    intent = store.arm(conn, session, 'old authorization', {'generation': 1}, 'router unavailable')
    return task, intent


def test_manual_retry_preserves_old_proof_and_clears_pending_claim(conn, project_root, monkeypatch):
    task, intent = parked(conn)
    monkeypatch.setenv('AGENTKIT_ROOT', str(project_root))
    monkeypatch.setattr(mcp_server, '_require_planner', lambda *a: None)
    assert 'requeued' in mcp_server.task_requeue(task, 'Host fixed reserved port; fresh guarded assessment')
    old = store.get(conn, intent['id'])
    assert old['state'] == 'CANCELLED' and old['proof'] == {'generation': 1}
    assert recovery_runtime.pending(conn, task_id=task) is None
    assert db.get_task(conn, task)['status'] == 'READY'
    assert conn.execute('SELECT count(*) FROM processes').fetchone()[0] == 0


def test_live_owner_prevents_retiring_claim(conn, monkeypatch):
    task, intent = parked(conn)
    monkeypatch.setattr(processes, 'owning', lambda c: [{'task_id': task}])
    with pytest.raises(ValueError, match='ownership must be stopped'):
        recovery_runtime.supersede_worker_intents(conn, task, 'Do not race the old agent')
    assert store.get(conn, intent['id'])['state'] == 'WAITING_AVAILABILITY'


def test_ordinary_ready_task_cannot_be_requeued_again(conn, project_root, monkeypatch):
    task = db.create_task(conn, title='ordinary', status='READY')
    monkeypatch.setenv('AGENTKIT_ROOT', str(project_root))
    monkeypatch.setattr(mcp_server, '_require_planner', lambda *a: None)
    with pytest.raises(ValueError, match='does not need recovery'):
        mcp_server.task_requeue(task, 'No stale recovery exists')
