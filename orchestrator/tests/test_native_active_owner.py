"""An absent view owner may be replaced while observing the exact active turn."""
import pytest
import test_native_session_recovery as helpers

from agentkit import native_owner_handoff
from agentkit import recovery_store as store

environment = helpers.environment


def _reload(native, monkeypatch, reply='no-client-found'):
    original = native.request

    def request(method, version, params, owner=None):
        if method == 'thread-owner-discovery':
            assert owner == 'owner'
            if reply == 'alive':
                return {'resultType': 'success', 'handledByClientId': owner}
            return {'resultType': 'error', 'error': reply, 'handledByClientId': None}
        return original(method, version, params, owner)

    monkeypatch.setattr(native, 'request', request)
    native.owner = 'reloaded-owner'


def test_active_owner_reload_keeps_watch_then_recovers_exact_quota(environment, monkeypatch):
    _, conn, native, _, step, checks, _ = environment
    _reload(native, monkeypatch)
    value = step()
    assert value['enabled'] and value['status'] == 'active_no_wake'
    assert value['owner'] == 'reloaded-owner'
    assert not checks and not native.deliveries and not value.get('intent_id')
    native.value = helpers.state(status='failed', runtime='systemError', quota=True)
    waiting = step(helpers.exhausted)
    assert waiting['status'] == 'waiting_for_provider_reset'
    assert store.get(conn, waiting['intent_id'])['proof']['owner'] == 'reloaded-owner'
    assert step()['status'] == 'accepted_completion_unverified'
    assert len(native.deliveries) == 1
    native.value = helpers.state('delivered-1', 'completed', 'idle')
    assert step()['native_turn_completed']


@pytest.mark.parametrize('fence', ['new_turn', 'pending', 'completed', 'interrupted',
    'peer', 'alive', 'unknown', 'submitted', 'intent', 'capacity'])
def test_active_owner_reload_never_extends_authorization(environment, monkeypatch, fence):
    _, _, native, target, step, checks, _ = environment
    _reload(native, monkeypatch, fence if fence in ('alive', 'unknown') else 'no-client-found')
    if fence == 'new_turn':
        native.value = helpers.state('different-user-turn')
    elif fence == 'pending':
        native.value['requests'] = ['approval']
    elif fence in ('completed', 'interrupted'):
        native.value = helpers.state(status=fence, runtime='idle')
    elif fence == 'peer':
        native.peer = {**native.peer, 'created_filetime': 999}
    elif fence in ('submitted', 'intent', 'capacity'):
        import json
        value = json.loads(target.read_text())
        field = {'submitted': 'submitted_turn', 'intent': 'intent_id',
                 'capacity': 'capacity_retry_intent'}[fence]
        value[field] = 'prior-delivery'
        native_owner_handoff.save(target, value)
    value = step()
    assert not value['enabled'] and not checks and not native.deliveries


def test_active_to_quota_during_handoff_retries_without_delivery(environment, monkeypatch):
    _, _, native, _, step, checks, _ = environment
    _reload(native, monkeypatch)

    def transition(count):
        if count == 3:  # registration, first reloaded snapshot, stable recheck
            native.value = helpers.state(status='failed', runtime='systemError', quota=True)

    native.on_snapshot = transition
    value = step()
    assert value['enabled'] and value['owner'] == 'owner'
    assert value['status'] == 'native_transport_temporarily_unavailable'
    assert not checks and not native.deliveries
    native.on_snapshot = None
    assert step(helpers.exhausted)['status'] == 'waiting_for_provider_reset'


def test_user_turn_during_handoff_is_revoked_not_retried(environment, monkeypatch):
    _, _, native, _, step, checks, _ = environment
    _reload(native, monkeypatch)
    native.on_snapshot = lambda count: setattr(native, 'value', helpers.state('new-user')) if count == 3 else None
    assert not step()['enabled']
    assert not checks and not native.deliveries
