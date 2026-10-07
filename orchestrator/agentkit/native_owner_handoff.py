"""Rebind a disconnected view only within the exact authorized native turn."""
from __future__ import annotations

from . import recovery_store as store
from .native_ipc import overview
from .native_session_registration import save


class NativeOwnerFence(PermissionError):
    """A fixed, non-private refusal reason safe for the recovery status view."""


def _eligible(summary, value):
    if (summary['latest_turn_id'] != value['anchor_turn'] or summary['pending']
            or value.get('submitted_turn')):
        raise NativeOwnerFence('Changed native owner does not match the exact authorized turn')
    stopped = (summary['latest_status'] == 'failed' and summary.get('quota_failure')
               and summary['runtime'] in ('idle', 'systemError'))
    watching = (summary['latest_status'] == 'inProgress' and summary['runtime'] == 'active'
                and not value.get('intent_id') and not value.get('capacity_retry_intent'))
    if not (stopped or watching):
        raise NativeOwnerFence('Changed native owner is neither an unclaimed active turn nor stopped quota')


def snapshot(conn, connection, value, target):
    if connection.current_peer() != value['pipe_peer']:
        raise NativeOwnerFence('Native pipe process changed; registration revoked')
    owner = connection.discover_owner(value['thread'])
    if owner == value['owner']:
        return overview(connection.snapshot(owner, value['thread']))
    summary = overview(connection.snapshot(owner, value['thread']))
    _eligible(summary, value)
    previous = value['owner']
    absent = connection.request('thread-owner-discovery', 1,
        {'hostId': 'local', 'conversationId': value['thread']}, previous)
    if (absent.get('resultType') != 'error' or absent.get('error') != 'no-client-found'
            or absent.get('handledByClientId') is not None):
        raise NativeOwnerFence('Previous native owner is not proven disconnected')
    if connection.current_peer() != value['pipe_peer'] or connection.discover_owner(value['thread']) != owner:
        raise NativeOwnerFence('Native identity changed during owner handoff')
    fresh = overview(connection.snapshot(owner, value['thread']))
    _eligible(fresh, value)
    if fresh != summary:
        # An active turn may reach its real quota failure between reads. No
        # submission happened here; retry observation with all fences intact.
        raise TimeoutError('Exact native turn changed state during owner observation')
    intent = store.get(conn, value.get('intent_id', ''))
    if intent:
        fields = ('thread', 'job_id', 'revision', 'anchor_turn', 'pipe_peer')
        if (intent['session_id'] != 'native-service:' + value['thread']
                or intent['authorization'] != value['anchor_turn']
                or intent['state'] not in ('WAITING_AVAILABILITY', 'READY_TO_WAKE')
                or intent['attempt_id'] or intent['failure'] != 'native_usage_limit'
                or any(intent['proof'].get(k) != value[k] for k in fields)
                or intent['proof'].get('owner') not in (previous, owner)):
            raise NativeOwnerFence('Native owner handoff cannot alter a claimed or changed intent')
        # A crash after this transaction is safe: the same guarded handoff accepts
        # either the old or already-rebound owner, before saving its file receipt.
        original = store.encode(intent['proof'])
        proof = {**intent['proof'], 'owner': owner}
        with store.transaction(conn):
            updated = conn.execute("UPDATE recovery_intents SET proof=? WHERE id=? AND proof=? "
                "AND state IN ('WAITING_AVAILABILITY','READY_TO_WAKE') AND attempt_id IS NULL",
                (store.encode(proof), intent['id'], original))
            if updated.rowcount != 1:
                raise NativeOwnerFence('Native recovery claim changed during owner handoff')
            store.journal(conn, intent['id'], intent['state'],
                          'Disconnected native owner rebound for the exact stopped quota turn')
    value['owner'] = owner
    value['owner_handoffs'] = [*value.get('owner_handoffs', []),
        {'old_owner': previous, 'new_owner': owner, 'turn': value['anchor_turn']}][-8:]
    save(target, value)
    return fresh
