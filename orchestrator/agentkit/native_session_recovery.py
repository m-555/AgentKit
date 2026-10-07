"""Finite supervisor-driven native recovery; quota failure is never idle authorization."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db, jobs, manager, wake_adapters
from . import recovery_store as store
from .locking import atomic_write, exclusive
from .native_ipc import Connection
from .native_session_registration import save
from .native_wake import _availability, delivery_payload, quota_summary
from .native_wake_control import require_enabled


def _stop(conn, value, reason):
    intent = store.get(conn, value.get('intent_id', ''))
    if intent and intent['state'] not in store.TERMINAL:
        store.cancel(conn, intent['id'], reason)
    value.update(enabled=False, status='needs_user_action', reason=reason)


def _authorized(conn, root, value):
    require_enabled(root, value['thread'])
    job = jobs.load(root, value['job_id'])
    runtime = conn.execute('SELECT * FROM jobs WHERE id=?', (value['job_id'],)).fetchone()
    lease = manager.lease(conn, value['job_id'])
    return bool(runtime and runtime['status'] == 'ACTIVE'
                and runtime['planned_revision'] == job['revision'] == value['revision']
                and lease and not lease['released_at'] and lease['provider'] == 'codex'
                and lease['session_ref'] == value['thread'])


def _snapshot(connection, value, conn, target):
    from .native_owner_handoff import snapshot
    return snapshot(conn, connection, value, target)


def _prompt(root, target, value):
    return (f'AgentKit registered quota recovery for this existing chat. Read {target} and '
            f'{root / ".ai/runtime/manager-checkpoint.json"}, then current user instructions, '
            f'project policy and authoritative job {value["job_id"]} state. '
            'Audit and acknowledge manager recovery before assigning or integrating work. '
            'Resume only current user-authorized work. Respect pause, readiness, roles, models, '
            'session caps and existing features. This transport is experimental. '
            'Delivery acceptance or turn start does not prove successful completion.')


def _observe(conn, value, summary, now):
    intent = store.get(conn, value.get('intent_id', ''))
    if not intent or intent['state'] not in store.DELIVERED:
        return
    submitted = value.get('submitted_turn')
    if not submitted:
        value.update(enabled=False, status='delivery_outcome_unknown_no_retry')
        if intent['state'] != 'RECONCILING':
            store.move(conn, intent['id'], 'RECONCILING', 'Submission receipt missing; never resend')
        return
    if summary['latest_turn_id'] != submitted:
        _stop(conn, value, 'An intervening user turn changed authorization')
        return
    attempt = intent['attempt_id']
    if not intent['started_at']:
        store.move(conn, intent['id'], 'TURN_STARTED', 'Exact submitted native turn observed', attempt_id=attempt)
    if summary['latest_status'] == 'completed':
        store.move(conn, intent['id'], 'RECOVERED', 'Exact submitted native turn completed', attempt_id=attempt)
        value.update(enabled=False, status='completed_waiting_for_user', native_turn_completed=True)
    elif summary['latest_status'] == 'interrupted':
        _stop(conn, value, 'User interrupted the delivered turn; never auto-resume')
    elif summary['latest_status'] == 'failed':
        evidence = {**intent['evidence'], 'failed_turn': submitted,
                    'capacity_failure': summary.get('capacity_failure') is True}
        store.move(conn, intent['id'], 'NEEDS_USER_ACTION', 'Delivered turn failed; completion not proven',
                   attempt_id=attempt, evidence=evidence)
        if summary.get('capacity_failure'):
            count = int(intent['proof'].get('capacity_retry_count', 0)) + 1
            if count > 3:
                value.update(enabled=False, status='capacity_retry_budget_exhausted')
                return
            value.pop('intent_id', None)
            value.pop('submitted_turn', None)
            value.update(capacity_retry_intent=intent['id'], capacity_retries=count,
                         capacity_retry_at=(now + timedelta(seconds=60 * 2 ** (count - 1))).isoformat(),
                         status='waiting_for_model_capacity', native_turn_completed=False)
            return
        # A new real quota failure starts its own intent on the next finite pass.
        if summary.get('quota_failure'):
            value.pop('intent_id', None)
            value.pop('submitted_turn', None)
            for key in ('capacity_retry_intent', 'capacity_retry_at', 'capacity_retries'):
                value.pop(key, None)
            value.update(status='waiting_for_next_quota_recovery', native_turn_completed=False)
        else:
            value.update(enabled=False, status='non_quota_failure_needs_user_action')


def _step(conn, root, target, value, connection, availability, now):
    if not _authorized(conn, root, value):
        _stop(conn, value, 'Job revision, readiness, pause or manager authority changed')
        return
    summary = _snapshot(connection, value, conn, target)
    value.update(last_snapshot=summary, observed_at=now.isoformat())
    if value.get('submitted_turn') or (store.get(conn, value.get('intent_id', '')) or {}).get('state') in store.DELIVERED:
        _observe(conn, value, summary, now)
        if (value.get('enabled') and summary['runtime'] == 'active'
                and summary['latest_status'] == 'inProgress' and value.get('submitted_turn') == summary['latest_turn_id']):
            lease = manager.lease(conn, value['job_id'])
            if lease and manager.pid_alive(lease['pid']):
                conn.execute('UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?',
                             (now.isoformat(), value['job_id']))
                value['next_check_at'] = (now + timedelta(seconds=60)).isoformat()
        return
    if summary['latest_turn_id'] != value['anchor_turn']:
        _stop(conn, value, 'An intervening user turn changed authorization; explicitly renew after reading it')
        return
    if summary['latest_status'] == 'interrupted':
        _stop(conn, value, 'User interrupted the authorized turn')
        return
    if summary['latest_status'] == 'completed':
        value['status'] = 'completed_waiting_for_user'
        return
    if summary['latest_status'] != 'failed':
        value['status'] = 'active_no_wake'
        if summary['runtime'] == 'active' and summary['latest_status'] == 'inProgress':
            lease = manager.lease(conn, value['job_id'])
            if lease and manager.pid_alive(lease['pid']):
                # Exact native peer, owner, turn and lease binding were checked above.
                # Code renews only an observed active turn, never a sleeping quota owner.
                if not manager.fresh(lease, now):
                    from .manager_state import record_outage
                    record_outage(conn, value['job_id'], 'codex', 'Verified native active turn renewed an expired heartbeat')
                conn.execute('UPDATE manager_leases SET heartbeat_at=? WHERE job_id=?',
                             (now.isoformat(), value['job_id']))
                value['next_check_at'] = (now + timedelta(seconds=60)).isoformat()
        return
    capacity = value.get('capacity_retry_intent')
    if capacity:
        prior = store.get(conn, capacity)
        fields = ('thread', 'job_id', 'revision', 'owner', 'pipe_peer')
        if (not prior or prior['session_id'] != 'native-service:' + value['thread']
                or prior['state'] != 'NEEDS_USER_ACTION'
                or prior['failure'] not in ('native_usage_limit', 'native_capacity_after_quota')
                or prior['evidence'].get('capacity_failure') is not True
                or prior['evidence'].get('failed_turn') != value['anchor_turn']
                or any(prior['proof'].get(k) != value[k] for k in fields)
                or value.get('capacity_retries') != int(prior['proof'].get('capacity_retry_count', 0)) + 1
                or not 1 <= value['capacity_retries'] <= 3 or not summary.get('capacity_failure')):
            _stop(conn, value, 'Capacity retry lacks exact failed quota-delivery evidence')
            return
        due = db.parse_ts(value.get('capacity_retry_at'))
        if not due or now < due:
            value['status'] = 'waiting_for_model_capacity'
            return
    elif not summary.get('quota_failure'):
        _stop(conn, value, 'Native failure is not a provider-confirmed usage limit')
        return
    from .manager_state import record_outage
    record_outage(conn, value['job_id'], 'codex', 'Native manager capacity after quota; audit required on return'
                  if capacity else 'Native manager usageLimitExceeded; audit required on return')
    proof = {k: value[k] for k in ('thread', 'job_id', 'revision', 'anchor_turn', 'owner', 'pipe_peer')}
    if capacity:
        proof.update(capacity_retry_intent=capacity, capacity_retry_count=value['capacity_retries'])
    intent = store.arm(conn, 'native-service:' + value['thread'], value['anchor_turn'], proof,
                       'native_capacity_after_quota' if capacity else 'native_usage_limit')
    value.update(intent_id=intent['id'], native_quota_failure_observed=True, status='waiting_for_provider_reset')
    if intent['state'] in store.TERMINAL:
        value.update(enabled=False, status='previous_intent_terminal_needs_user_action')
        return
    lease = manager.lease(conn, value['job_id'])
    if not lease or manager.fresh(lease, now) or summary['pending'] or summary['runtime'] not in ('idle', 'systemError'):
        value['status'] = 'waiting_for_stopped_native_owner'
        return
    quota = quota_summary(availability())
    value.update(latest_quota=quota, last_quota_observed_at=now.isoformat())
    if not quota['provider_confirmed_available']:
        return
    # Re-read every authority fence after metadata I/O and immediately before claiming.
    fresh = _snapshot(connection, value, conn, target)
    lease = manager.lease(conn, value['job_id'])
    if not _authorized(conn, root, value) or not lease or manager.fresh(lease, now):
        return
    if (fresh['latest_turn_id'] != value['anchor_turn'] or fresh['latest_status'] != 'failed'
            or not fresh.get('capacity_failure' if capacity else 'quota_failure')
            or fresh['pending'] or fresh['runtime'] not in ('idle', 'systemError')):
        return
    packet = {'job': value['job_id'], 'revision': value['revision'], 'intent': intent['id'],
              'checkpoint': str(root / '.ai/runtime/manager-checkpoint.json'),
              'native_owner_stopped': True, 'manager_lease_expired': True,
              'requires_recovery_audit_on_return': True}
    atomic_write(root / '.ai/runtime' / ('wake-audit-' + intent['id'] + '.json'), store.encode(packet))
    evidence = {'observed_at': value['last_quota_observed_at'], 'available': True,
                'complete': quota['complete'], 'windows': quota['windows']}
    if not wake_adapters.ready(conn, intent['id'], evidence, authorized=True,
                               old_owner_stopped=True, proof=proof, audit_packet=True, now=now):
        return
    attempt = store.claim(conn, intent['id'], 'native-supervisor-service', proof=proof,
                          authorization=value['anchor_turn'], now=now)
    if not attempt:
        return
    value.update(status='delivery_claimed', attempt_id=attempt)
    save(target, value)  # Claim and receipt must survive crash before the IPC request.
    try:
        reply = connection.request('thread-follower-start-turn', 2,
            delivery_payload(value['thread'], _prompt(root, target, value), str(uuid.uuid4())), value['owner'])
        turn = (reply.get('result') or {}).get('result', {}).get('turn', {})
        if (reply.get('resultType') != 'success' or reply.get('method') != 'thread-follower-start-turn'
                or reply.get('handledByClientId') != value['owner'] or not turn.get('id')):
            raise RuntimeError('Native delivery rejected or reply identity unknown')
        store.move(conn, intent['id'], 'DELIVERY_ACCEPTED', 'Exact native delivery accepted; completion unverified', attempt_id=attempt)
        value.update(submitted_turn=turn['id'], anchor_turn=turn['id'],
                     status='accepted_completion_unverified', native_turn_completed=False)
        save(target, value)
    except Exception:
        store.move(conn, intent['id'], 'RECONCILING', 'Delivery outcome unknown; never resend', attempt_id=attempt)
        value.update(enabled=False, status='delivery_outcome_unknown_no_retry')


def tick(conn, root, *, connect=Connection, availability=_availability, now=None):
    """No model probes while active/idle; one bounded pass per registered session/300s."""
    root = Path(root).resolve()
    moment = now or datetime.now(UTC)
    notes = []
    for target in sorted((root / '.ai/runtime/native-session-recovery').glob('*.json')):
        connection = None
        value = None
        try:
            with exclusive(root, 'native-session-' + target.stem, timeout=0):
                value = json.loads(target.read_text(encoding='utf-8'))
                due = db.parse_ts(value.get('next_check_at'))
                if not value.get('enabled') or (due and due > moment):
                    continue
                value['next_check_at'] = (moment + timedelta(seconds=300)).isoformat()
                try:
                    connection = connect()
                    connection.initialize()
                except (FileNotFoundError, BrokenPipeError, ConnectionResetError) as error:
                    # No turn delivery can have happened during connection setup.
                    # Preserve authorization; every later pass rechecks identity.
                    raise TimeoutError("Native transport temporarily unavailable") from error
                _step(conn, root, target, value, connection, availability, moment)
                save(target, value)
                notes.append('Native recovery: ' + value['status'])
        except TimeoutError:
            # Busy registration or temporary missing IPC is not permission to deliver.
            if value is not None:
                value['status'] = 'native_transport_temporarily_unavailable'
                save(target, value)
        except sqlite3.OperationalError as error:
            if not any(word in str(error).lower() for word in ('locked', 'busy')):
                raise
            if value is not None:
                value['status'] = 'waiting_for_database_lock'
                save(target, value)  # Preserve intent/claim; never revoke or deliver on contention.
        except Exception as error:
            if value is not None:
                from .native_owner_handoff import NativeOwnerFence
                reason = str(error) if isinstance(error, NativeOwnerFence) else type(error).__name__
                _stop(conn, value, 'Native recovery fenced: ' + reason)
                save(target, value)
                notes.append('Native recovery needs attention')
        finally:
            if connection is not None:
                if value is not None:
                    with suppress(Exception):
                        connection.following(value['owner'], value['thread'], False)
                connection.close()
    return notes
