"""Host-authorized, durable quota-only recovery for an existing native manager."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import suppress
from pathlib import Path

from . import db, jobs, manager, manager_state, recovery_store
from .locking import atomic_write, exclusive
from .native_ipc import Connection, overview
from .native_wake_control import require_enabled


def path(root, thread):
    key = hashlib.sha256(thread.encode()).hexdigest()[:24]
    return Path(root) / '.ai/runtime/native-session-recovery' / (key + '.json')


def save(target, value):
    atomic_write(target, recovery_store.encode(value) + '\n')


def register(root, job_id, thread, token, *, connect=Connection):
    root = Path(root).resolve()
    if os.environ.get('AGENTKIT_PROCESS') or os.environ.get('AGENTKIT_TASK'):
        raise PermissionError('Only the external manager/operator can register native recovery')
    if not thread or os.environ.get('CODEX_THREAD_ID') != thread:
        raise PermissionError('Register only the currently authorizing native chat')
    require_enabled(root, thread)
    conn = db.connect(root)
    connection = None
    owner = None
    try:
        lease = manager.require_lease(conn, job_id, token)
        if lease['provider'] != 'codex' or lease['session_ref'] != thread:
            raise PermissionError('Native recovery must match the current manager lease')
        manager_state.require_clear(conn, job_id)
        job = jobs.load(root, job_id)
        runtime = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not runtime or runtime['status'] != 'ACTIVE' or runtime['planned_revision'] != job['revision']:
            raise PermissionError('Register only a ready, authorized active job')
        connection = connect()
        connection.initialize()
        owner = connection.discover_owner(thread)
        summary = overview(connection.snapshot(owner, thread))
        if summary['runtime'] != 'active' or summary['latest_status'] != 'inProgress':
            raise PermissionError('Register during the active authorizing turn')
        target = path(root, thread)
        with exclusive(root, 'native-session-' + target.stem):
            previous_session = recovery_store.session(conn, 'native-service:' + thread)
            if previous_session and previous_session['job_id'] != job_id:
                raise PermissionError('A native recovery registration cannot silently change jobs')
            if target.exists():
                old = json.loads(target.read_text(encoding='utf-8'))
                intent = recovery_store.get(conn, old.get('intent_id', ''))
                if intent and intent['state'] in recovery_store.DELIVERED:
                    raise PermissionError('Reconcile the previous delivery before replacing registration')
                if intent:
                    recovery_store.cancel(conn, intent['id'], 'New user turn explicitly renewed registration')
            # Independent service polls active identity every60s. Allow host I/O
            # margin, while failed quota turns still wait for lease expiry.
            conn.execute('UPDATE manager_leases SET ttl_seconds=MAX(ttl_seconds,300) WHERE job_id=?', (job_id,))
            value = {'thread': thread, 'job_id': job_id, 'revision': job['revision'],
                     'anchor_turn': summary['latest_turn_id'], 'owner': owner,
                     'pipe_peer': connection.current_peer(), 'enabled': True,
                     'status': 'registered_waiting_for_quota_failure', 'next_check_at': None,
                     'registered_at': db.utcnow(), 'private_protocol': True,
                     'production_guarantee': False, 'quota_only': True}
            recovery_store.register(conn, identifier='native-service:' + thread, provider='codex',
                account='default', host='codex-vscode', role='manager', reference=thread,
                job_id=job_id, identity={'owner': owner, 'pipe_peer': value['pipe_peer']})
            save(target, value)
        from .native_recovery_service import start
        start(root)
        return value
    finally:
        if connection is not None:
            if owner:
                with suppress(Exception):
                    connection.following(owner, thread, False)
            connection.close()
        conn.close()


def snapshot(root):
    """Expose bounded status only; never IPC, credentials, requests or conversation text."""
    rows = []
    fields = ('thread', 'job_id', 'enabled', 'status', 'reason', 'registered_at',
              'observed_at', 'next_check_at', 'private_protocol', 'production_guarantee',
              'native_quota_failure_observed', 'native_turn_completed')
    for target in sorted((Path(root) / '.ai/runtime/native-session-recovery').glob('*.json')):
        try:
            value = json.loads(target.read_text(encoding='utf-8'))
            rows.append({key: value.get(key) for key in fields})
        except (OSError, ValueError, AttributeError):
            rows.append({'enabled': False, 'status': 'invalid_registration_needs_user_action'})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--thread', required=True)
    parser.add_argument('--job')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args(argv)
    if args.status:
        value = json.loads(path(args.root, args.thread).read_text(encoding='utf-8'))
    else:
        if not args.job:
            parser.error('--job is required for registration')
        token = (args.root / '.ai/runtime' / f'manager-{args.job}.credential').read_text().strip()
        value = register(args.root, args.job, args.thread, token)
    print(json.dumps(value, ensure_ascii=True))


if __name__ == '__main__':
    main()
