"""Renewal needs a new active authorization and preserves old delivery receipts."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentkit import db, native_wake, native_wake_control, recovery_store
from tests.test_native_wake import Clock, Native, healthy, state


def test_new_authorized_turn_can_renew_completed_one_shot(tmp_path,monkeypatch):
    for key in ('AGENTKIT_TASK','AGENTKIT_PROCESS','CODEX_THREAD_ID'):
        monkeypatch.delenv(key,raising=False)
    clock=Clock()
    key=native_wake.hashlib.sha256(b'thread').hexdigest()[:24]
    result_path=tmp_path/'.ai/runtime/native-wake'/f'{key}.result.json'

    def run(turn,renew=False):
        native=Native(clock)
        native.result_path=result_path
        original=native.snapshot
        native.after_arm=lambda:state(turn=turn)
        def snapshot(owner,thread):
            response=original(owner,thread)
            if native.snapshots==1:
                response['turns'][0]['turnId']=turn
            return response
        native.snapshot=snapshot
        result=native_wake.run(tmp_path,'thread',clock.now()+timedelta(seconds=3),
            clock.now()+timedelta(seconds=30),1,renew=renew,connect=lambda:native,
            availability=healthy,now=clock.now,pause=clock.sleep)
        return result,native

    first,a=run('first')
    second,b=run('second',renew=True)
    assert first['native_turn_completed'] and second['native_turn_completed']
    assert len(a.deliveries)==len(b.deliveries)==1
    assert first['recovery_intent_id'] != second['recovery_intent_id']
    assert len(list((result_path.parent/'archive').glob('*/'+result_path.name)))==1
    c=db.connect(tmp_path)
    try:
        rows=recovery_store.snapshot(c)
        assert len(rows)==2 and all(row['state']=='RECOVERED' for row in rows)
    finally:
        c.close()


@pytest.mark.parametrize('status',['native_turn_accepted_completion_unverified','delivery_outcome_unknown_no_retry','armed_waiting'])
def test_unresolved_delivery_cannot_be_renewed(tmp_path,status):
    folder=tmp_path/'wake'
    folder.mkdir()
    result=folder/'key.result.json'
    result.write_text(json.dumps({'status':status}))
    with pytest.raises(RuntimeError,match='unresolved'):
        native_wake_control.archive_finished(tmp_path,folder,'key')
    assert result.exists() and not (folder/'archive').exists()


def test_scoped_wake_uses_project_checkpoint_and_preserves_old_receipt(tmp_path, monkeypatch):
    for name in ('AGENTKIT_TASK', 'AGENTKIT_PROCESS', 'CODEX_THREAD_ID'):
        monkeypatch.delenv(name, raising=False)
    clock = Clock()
    native = Native(clock)
    key = native_wake.hashlib.sha256(b'thread').hexdigest()[:24]
    folder = tmp_path / '.ai/runtime/native-wake'
    folder.mkdir(parents=True)
    old = folder / f'{key}.result.json'
    old.write_text('{"status":"delivery_outcome_unknown_no_retry"}')
    before = old.read_bytes()
    native.result_path = folder / 'scopes/new-authorized-test' / old.name
    result = native_wake.run(tmp_path, 'thread', clock.now() + timedelta(seconds=3),
        clock.now() + timedelta(seconds=30), 1, scope='new-authorized-test',
        connect=lambda: native, availability=healthy, now=clock.now, pause=clock.sleep)
    assert result['native_turn_completed'] and len(native.deliveries) == 1
    prompt = native.deliveries[0][1]['turnStart']['request']['input'][0]['text']
    assert str(tmp_path / '.ai/runtime/manager-checkpoint.json') in prompt
    assert old.read_bytes() == before


@pytest.mark.parametrize('scope', ['../escape', 'a/b', 'x' * 81])
def test_scope_refuses_path_traversal_before_transport(tmp_path, scope, monkeypatch):
    for name in ('AGENTKIT_TASK', 'AGENTKIT_PROCESS', 'CODEX_THREAD_ID'):
        monkeypatch.delenv(name, raising=False)
    clock = Clock()
    with pytest.raises(ValueError, match='filename-safe'):
        native_wake.run(tmp_path, 'thread', clock.now() + timedelta(seconds=3),
            clock.now() + timedelta(seconds=30), scope=scope,
            now=clock.now, connect=lambda: pytest.fail('Transport must not start'))
