"""Persistent recovery qualification without providers, native IPC, or model turns."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, jobs, manager, manager_state, models
from agentkit import native_session_recovery as recovery
from agentkit import native_session_registration as registration
from agentkit import recovery_store as store
from agentkit.native_ipc import overview


def state(turn='authorized', status='inProgress', runtime='active', *, quota=False, pending=False):
    error = {'codexErrorInfo': 'usageLimitExceeded', 'message': 'PRIVATE_ERROR_TEXT'} if quota else None
    return {'threadRuntimeStatus': {'type': runtime}, 'requests': ['approval'] if pending else [],
            'unconfirmedTurnSubmissions': [],
            'turns': [{'turnId': turn, 'status': status, 'error': error}]}


class Native:
    def __init__(self):
        self.value = state()
        self.owner = 'owner'
        self.peer = {'pid': 123, 'created_filetime': 456, 'executable': 'Code.exe'}
        self.deliveries = []
        self.failure = False
        self.snapshot_calls = 0
        self.on_snapshot = None

    def initialize(self):
        pass

    def current_peer(self):
        return self.peer

    def discover_owner(self, thread):
        return self.owner

    def snapshot(self, owner, thread):
        self.snapshot_calls += 1
        if self.on_snapshot:
            self.on_snapshot(self.snapshot_calls)
        return self.value

    def request(self, method, version, params, owner):
        if method == 'thread-owner-discovery':
            return {'resultType': 'success', 'handledByClientId': owner}
        self.deliveries.append(params)
        if self.failure:
            raise TimeoutError('Lost reply')
        turn = 'delivered-' + str(len(self.deliveries))
        self.value = state(turn)
        return {'resultType': 'success', 'method': method, 'handledByClientId': owner,
                'result': {'result': {'turn': {'id': turn}}}}

    def following(self, owner, thread, enabled):
        assert enabled is False

    def close(self):
        pass


@pytest.fixture
def environment(project_root, conn, monkeypatch):
    for key in ('AGENTKIT_TASK', 'AGENTKIT_PROCESS'):
        monkeypatch.delenv(key, raising=False)
    from agentkit import native_recovery_service
    monkeypatch.setattr(native_recovery_service, 'start', lambda _root: 'fake host service')
    monkeypatch.setenv('CODEX_THREAD_ID', 'native-thread')
    job = jobs.create(project_root, 'native', 'Continue this authorized job', 'codex')
    pin = models.Profile('sol', 'codex', 'gpt-6.1-sol', 'xhigh', 1)
    jobs.pin_coordinator(project_root, job['id'], pin)
    token = manager.attach(conn, project_root, job['id'], 'native', os.getpid(), session_ref='native-thread')
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=1 WHERE id='native'")
    native = Native()
    registration.register(project_root, 'native', 'native-thread', token, connect=lambda: native)
    moment = datetime.now(UTC)
    target = registration.path(project_root, 'native-thread')
    checks = []

    def available():
        checks.append(1)
        return {'complete': True, 'available': True, 'windows': [{'window': '300', 'used_percent': 0}]}

    def step(checker=available):
        nonlocal moment
        moment += timedelta(seconds=301)
        recovery.tick(conn, project_root, connect=lambda: native, availability=checker, now=moment)
        return json.loads(target.read_text())

    return project_root, conn, native, target, step, checks, token


def exhausted():
    return {'complete': True, 'available': False, 'windows': [{'window': '10080', 'used_percent': 100}]}


def test_two_quota_cycles_persist_exact_receipts_and_never_claim_idle(environment):
    root, conn, native, target, step, checks, _ = environment
    assert step()['status'] == 'active_no_wake'
    assert not checks and not native.deliveries
    native.value = state(status='failed', runtime='systemError', quota=True)
    assert step(exhausted)['status'] == 'waiting_for_provider_reset'
    value = step()
    assert value['status'] == 'accepted_completion_unverified'
    first = value['intent_id']
    assert store.get(conn, first)['state'] == 'DELIVERY_ACCEPTED'
    assert not value['native_turn_completed']
    assert step()['status'] == 'accepted_completion_unverified'
    native.value = state('delivered-1', 'failed', 'systemError', quota=True)
    assert step()['status'] == 'waiting_for_next_quota_recovery'
    value = step()
    assert value['intent_id'] != first and len(native.deliveries) == 2
    assert store.get(conn, first)['state'] == 'NEEDS_USER_ACTION'
    native.value = state('delivered-2', 'completed', 'idle')
    value = step()
    assert value['native_turn_completed'] and not value['enabled']
    assert store.get(conn, value['intent_id'])['state'] == 'RECOVERED'
    assert step()['native_turn_completed'] and len(native.deliveries) == 2
    assert 'PRIVATE_ERROR_TEXT' not in target.read_text()
    assert len(list((root / '.ai/runtime').glob('wake-audit-*.json'))) == 2


@pytest.mark.parametrize('status,quota', [('completed', False), ('interrupted', True), ('failed', False)])
def test_idle_stop_and_nonquota_failure_never_resume(environment, status, quota):
    _, _, native, _, step, checks, _ = environment
    native.value = state(status=status, runtime='idle', quota=quota)
    value = step()
    assert not native.deliveries and not checks
    assert value['status'] in ('completed_waiting_for_user', 'needs_user_action')


@pytest.mark.parametrize('fence', ['pending', 'lease', 'new_turn', 'peer', 'owner', 'revision', 'pause', 'released'])
def test_authority_and_liveness_fences(environment, fence):
    root, conn, native, _, step, checks, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    if fence == 'pending':
        native.value['requests'] = ['approval']
    elif fence == 'lease':
        conn.execute('UPDATE manager_leases SET ttl_seconds=3600')
    elif fence == 'new_turn':
        native.value = state('user-intervened', 'failed', 'systemError', quota=True)
    elif fence == 'peer':
        native.peer = {**native.peer, 'created_filetime': 999}
    elif fence == 'owner':
        native.owner = 'different'
    elif fence == 'revision':
        jobs.amend(root, 'native', 'Changed instructions', user=True)
    elif fence == 'pause':
        config = root / '.ai/project.yaml'
        config.write_text(config.read_text() + '\nexecution_paused: true\n')
    elif fence == 'released':
        conn.execute("UPDATE manager_leases SET released_at=?", (db.utcnow(),))
    step()
    assert not native.deliveries and not checks


def test_authority_rechecked_after_slow_availability(environment):
    _, conn, native, _, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)

    def checker():
        conn.execute('UPDATE manager_leases SET ttl_seconds=3600')
        return {'complete': True, 'available': True, 'windows': [{'window': '300', 'used_percent': 0}]}

    step(checker)
    assert not native.deliveries


def test_lost_reply_survives_restart_without_duplicate_delivery(environment):
    root, conn, native, target, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    native.failure = True
    value = step()
    assert value['status'] == 'delivery_outcome_unknown_no_retry'
    assert store.get(conn, value['intent_id'])['state'] == 'RECONCILING'
    # Simulate a stale state file surviving a crash, while the ledger claim persists.
    value.update(enabled=True, submitted_turn=None)
    registration.save(target, value)
    step()
    assert len(native.deliveries) == 1
    native.value = state('user-returned')
    # A returning root first acknowledges its quota epoch, then still must
    # reconcile the unknown delivery rather than overwriting its claim.
    conn.execute('UPDATE manager_state SET acknowledged_epoch=epoch')
    with pytest.raises(PermissionError, match='Reconcile'):
        registration.register(root, 'native', 'native-thread', environment[-1], connect=lambda: native)


def test_delivered_user_interruption_never_resumes(environment):
    _, _, native, _, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    step()
    native.value = state('delivered-1', 'interrupted', 'idle', quota=True)
    assert not step()['enabled']
    assert len(native.deliveries) == 1


@pytest.mark.parametrize('quota', [None, {'available': True, 'complete': False},
    {'available': True, 'complete': True, 'windows': []},
    {'available': True, 'complete': True, 'windows': [{'used_percent': 100, 'window': 'weekly'}]}])
def test_unknown_or_weekly_blocked_availability_never_delivers(environment, quota):
    _, _, native, _, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    step(lambda: quota or {})
    assert not native.deliveries


def test_polling_coalesces_and_registration_requires_current_chat(environment, monkeypatch):
    root, conn, native, _, step, checks, token = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    step(exhausted)
    before = native.snapshot_calls
    recovery.tick(conn, root, connect=lambda: native, availability=lambda: pytest.fail('early probe'))
    assert native.snapshot_calls == before and not checks
    monkeypatch.setenv('CODEX_THREAD_ID', 'different-thread')
    with pytest.raises(PermissionError, match='currently authorizing'):
        registration.register(root, 'native', 'native-thread', token, connect=lambda: native)


def test_registration_refuses_unaudited_manager_return(environment):
    root, conn, native, _, _, _, token = environment
    manager_state.record_outage(conn, 'native', 'codex', 'Actual subscription limit')
    with pytest.raises(ValueError, match='recovery audit'):
        registration.register(root, 'native', 'native-thread', token, connect=lambda: native)


def test_only_explicit_provider_quota_code_is_persisted():
    assert overview(state(status='failed', quota=True))['quota_failure'] is True
    value = state(status='failed')
    value['turns'][0]['error'] = {'message': 'quota', 'codexErrorInfo': 'other'}
    assert not overview(value)['quota_failure']



def test_dashboard_exposes_registration_status_without_probe_or_private_state(environment):
    root, _, _, target, _, _, _ = environment
    value = json.loads(target.read_text())
    value['private_request'] = 'DO_NOT_SHOW'
    registration.save(target, value)
    rows = registration.snapshot(root)
    assert rows[0]['enabled'] and rows[0]['status'] == 'registered_waiting_for_quota_failure'
    assert 'DO_NOT_SHOW' not in json.dumps(rows)
    assert 'anchor_turn' not in rows[0] and 'owner' not in rows[0]
    target.write_text('invalid-json')
    assert registration.snapshot(root)[0]['status'] == 'invalid_registration_needs_user_action'



def test_native_quota_failure_fences_new_work_even_if_metadata_reset_was_missed(environment):
    _, conn, native, _, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    value = step()
    assert value['status'] == 'accepted_completion_unverified'
    assert manager_state.pending(conn, 'native')
    assert manager_state.state(conn, 'native')['epoch'] == 1
    step()
    assert manager_state.state(conn, 'native')['epoch'] == 1



def test_verified_active_turn_renews_lease_but_quota_failure_does_not(environment):
    _, conn, native, _, step, checks, _ = environment
    before = manager.lease(conn, 'native')['heartbeat_at']
    value = step()
    active = manager.lease(conn, 'native')['heartbeat_at']
    assert active != before and value['status'] == 'active_no_wake'
    assert not checks and not native.deliveries
    native.value = state(status='failed', runtime='systemError', quota=True)
    step(exhausted)
    assert manager.lease(conn, 'native')['heartbeat_at'] == active


def test_native_registration_has_margin_for_independent_poll_cadence(environment):
    _, conn, _, _, _, _, _ = environment
    assert manager.lease(conn, 'native')['ttl_seconds'] >= 300


def test_transient_database_contention_preserves_native_registration(environment, monkeypatch):
    import sqlite3
    root, conn, native, target, step, _, _ = environment
    before = json.loads(target.read_text())
    def blocked(*_args):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(recovery, '_step', blocked)
    result = step()
    assert result['enabled'] and result['status'] == 'waiting_for_database_lock'
    assert result['anchor_turn'] == before['anchor_turn']
    assert native.deliveries == []
