"""A reloaded native view may change owner only after its old client is proven absent."""
import json

import pytest
import test_native_session_recovery as helpers

from agentkit import recovery_store as store

environment = helpers.environment


def _waiting(environment, monkeypatch, old_reply='no-client-found'):
    _, _, native, _, step, _, _ = environment
    native.value = helpers.state(status='failed', runtime='systemError', quota=True)
    waiting = step(helpers.exhausted)
    original = native.request

    def request(method, version, params, owner=None):
        if method == 'thread-owner-discovery':
            assert owner == 'owner'
            if old_reply == 'alive':
                return {'resultType': 'success', 'handledByClientId': owner}
            return {'resultType': 'error', 'error': old_reply, 'handledByClientId': None}
        return original(method, version, params, owner)

    monkeypatch.setattr(native, 'request', request)
    native.owner = 'reloaded-owner'
    return waiting


def test_same_failed_quota_turn_with_absent_old_owner_can_resume(environment, monkeypatch):
    _, conn, native, target, step, _, _ = environment
    waiting = _waiting(environment, monkeypatch)
    resumed = step()
    assert resumed['enabled'] and resumed['owner'] == 'reloaded-owner'
    assert resumed['status'] == 'accepted_completion_unverified'
    assert len(native.deliveries) == 1 and not resumed['native_turn_completed']
    intent = store.get(conn, waiting['intent_id'])
    assert intent['proof']['owner'] == 'reloaded-owner'
    assert resumed['owner_handoffs'][-1]['old_owner'] == 'owner'
    native.value = helpers.state('delivered-1', 'completed', 'idle')
    assert step()['native_turn_completed']
    assert 'PRIVATE_ERROR_TEXT' not in target.read_text()


@pytest.mark.parametrize('fence', ['active', 'input', 'turn', 'peer', 'old_owner_unknown', 'old_owner_alive', 'claimed', 'forged_proof'])
def test_native_owner_handoff_preserves_delivery_fences(environment, monkeypatch, fence):
    _, conn, native, _, step, _, _ = environment
    replies = {'old_owner_unknown': 'timeout', 'old_owner_alive': 'alive'}
    waiting = _waiting(environment, monkeypatch, replies.get(fence, 'no-client-found'))
    if fence == 'active':
        native.value = helpers.state()
    elif fence == 'input':
        native.value['requests'] = ['approval']
    elif fence == 'turn':
        native.value = helpers.state('new-user-turn', 'failed', 'systemError', quota=True)
    elif fence == 'peer':
        native.peer = {**native.peer, 'created_filetime': 999}
    elif fence == 'claimed':
        conn.execute("UPDATE recovery_intents SET state='CLAIMED',attempt_id='other' WHERE id=?", (waiting['intent_id'],))
    elif fence == 'forged_proof':
        intent = store.get(conn, waiting['intent_id'])
        proof = {**intent['proof'], 'owner': 'different-client'}
        conn.execute('UPDATE recovery_intents SET proof=? WHERE id=?', (json.dumps(proof), waiting['intent_id']))
    value = step()
    assert not value['enabled'] and not native.deliveries


def test_rebinding_persists_before_availability_checks_and_survives_restart(environment, monkeypatch):
    _, conn, native, target, step, _, _ = environment
    first = _waiting(environment, monkeypatch)
    waiting = step(helpers.exhausted)
    assert waiting['enabled'] and waiting['owner'] == 'reloaded-owner'
    assert json.loads(target.read_text())['owner'] == 'reloaded-owner'
    assert store.get(conn, first['intent_id'])['proof']['owner'] == 'reloaded-owner'
    assert not native.deliveries
    assert step()['status'] == 'accepted_completion_unverified'
    assert len(native.deliveries) == 1


def test_crash_after_proof_rebind_before_file_receipt_reconciles_without_duplicate(environment, monkeypatch):
    from agentkit import native_owner_handoff as handoff
    _, conn, native, target, step, _, _ = environment
    waiting = _waiting(environment, monkeypatch)
    original = handoff.save
    count = 0

    def crash_once(*args):
        nonlocal count
        count += 1
        if count == 1:
            raise SystemExit('Simulated host crash before receipt write')
        return original(*args)

    monkeypatch.setattr(handoff, 'save', crash_once)
    with pytest.raises(SystemExit):
        step()
    assert json.loads(target.read_text())['owner'] == 'owner'
    assert store.get(conn, waiting['intent_id'])['proof']['owner'] == 'reloaded-owner'
    assert not native.deliveries
    assert step()['status'] == 'accepted_completion_unverified'
    assert len(native.deliveries) == 1
