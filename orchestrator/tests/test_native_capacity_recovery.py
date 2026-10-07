"""Known failed capacity turns after quota recovery may retry; unknown outcomes never do."""
import json

import pytest
import test_native_session_recovery as helpers

from agentkit import recovery_store as store

environment = helpers.environment
exhausted = helpers.exhausted
state = helpers.state


def _failed_capacity(turn):
    value = state(turn, 'failed', 'systemError')
    value['turns'][0]['error'] = {'codexErrorInfo': 'serverOverloaded', 'message': 'PRIVATE_ERROR_TEXT'}
    return value


def _quota_delivery(environment):
    _, _, native, _, step, _, _ = environment
    native.value = state(status='failed', runtime='systemError', quota=True)
    return step()


def test_confirmed_capacity_failure_retries_and_only_completion_proves_recovery(environment):
    _, conn, native, target, step, _, _ = environment
    first = _quota_delivery(environment)
    native.value = _failed_capacity('delivered-1')
    waiting = step()
    assert waiting['enabled'] and waiting['status'] == 'waiting_for_model_capacity'
    assert not waiting['native_turn_completed'] and len(native.deliveries) == 1
    assert store.get(conn, first['intent_id'])['state'] == 'NEEDS_USER_ACTION'
    resumed = step()
    assert resumed['intent_id'] != first['intent_id'] and len(native.deliveries) == 2
    assert not resumed['native_turn_completed']
    native.value = state('delivered-2', 'completed', 'idle')
    done = step()
    assert done['native_turn_completed'] and not done['enabled']
    assert store.get(conn, done['intent_id'])['state'] == 'RECOVERED'
    assert 'PRIVATE_ERROR_TEXT' not in target.read_text()


def test_capacity_retries_survive_restart_and_exhaust_a_finite_budget(environment):
    _, _, native, _, step, _, _ = environment
    _quota_delivery(environment)
    for attempt in range(1, 4):
        native.value = _failed_capacity('delivered-' + str(attempt))
        waiting = step()
        assert waiting['enabled'] and waiting['capacity_retries'] == attempt
        resumed = step()
        assert resumed['status'] == 'accepted_completion_unverified'
        assert len(native.deliveries) == attempt + 1
    native.value = _failed_capacity('delivered-4')
    stopped = step()
    assert not stopped['enabled'] and stopped['status'] == 'capacity_retry_budget_exhausted'
    step()
    assert len(native.deliveries) == 4


@pytest.mark.parametrize('fence', ['input', 'new_turn', 'pause', 'identity', 'quota', 'forged_proof'])
def test_capacity_retry_keeps_authority_and_quota_fences(environment, fence):
    _, conn, native, target, step, _, _ = environment
    _quota_delivery(environment)
    native.value = _failed_capacity('delivered-1')
    assert step()['enabled']
    if fence == 'input':
        native.value['requests'] = ['approval']
    elif fence == 'new_turn':
        native.value = state('user-changed-instructions', 'completed', 'idle')
    elif fence == 'pause':
        conn.execute("UPDATE jobs SET status='BLOCKED' WHERE id='native'")
    elif fence == 'identity':
        native.peer = {**native.peer, 'created_filetime': 999}
    elif fence == 'forged_proof':
        saved = json.loads(target.read_text())
        saved['capacity_retry_intent'] = 'not-a-delivered-intent'
        target.write_text(json.dumps(saved))
    step(exhausted if fence == 'quota' else lambda: {'complete': True, 'available': True, 'windows': [{'window': '300', 'used_percent': 0}]})
    assert len(native.deliveries) == 1


def test_capacity_without_a_previous_quota_delivery_never_authorizes_resume(environment):
    _, _, native, _, step, checks, _ = environment
    native.value = _failed_capacity('authorized')
    assert not step()['enabled']
    assert not native.deliveries and not checks


def test_capacity_retry_waits_for_backoff_without_probing_or_delivering(environment):
    from datetime import UTC, datetime, timedelta

    from agentkit import native_session_recovery as recovery
    from agentkit.native_session_registration import save
    root, conn, native, target, step, checks, _ = environment
    _quota_delivery(environment)
    native.value = _failed_capacity('delivered-1')
    waiting = step()
    waiting['next_check_at'] = None
    waiting['capacity_retry_at'] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    save(target, waiting)
    before = len(checks)
    recovery.tick(conn, root, connect=lambda: native, availability=lambda: pytest.fail('Premature probe'))
    assert len(native.deliveries) == 1 and len(checks) == before
