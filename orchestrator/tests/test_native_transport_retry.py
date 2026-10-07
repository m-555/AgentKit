"""Temporary native pipe loss must not revoke durable quota authorization."""
import json

import pytest
from test_native_session_recovery import environment as _environment
from test_native_session_recovery import state

from agentkit import native_session_recovery as recovery

environment = _environment


@pytest.mark.parametrize('error',[FileNotFoundError('native pipe missing'),BrokenPipeError('pipe closed'),ConnectionResetError('pipe reset')])
def test_missing_native_connection_retains_registration_and_retries(environment,error):
    root,conn,native,target,step,checks,_=environment
    def unavailable():
        raise error
    recovery.tick(conn,root,connect=unavailable)
    value=json.loads(target.read_text())
    assert value['enabled'] is True
    assert value['status']=='native_transport_temporarily_unavailable'
    assert not checks and not native.deliveries
    native.value=state(status='failed',runtime='systemError',quota=True)
    value=step()
    assert value['status']=='accepted_completion_unverified'
    assert len(native.deliveries)==1


def test_denied_native_connection_still_fences_authority(environment):
    root,conn,native,target,_,checks,_=environment
    def denied():
        raise PermissionError('private access detail')
    recovery.tick(conn,root,connect=denied)
    value=json.loads(target.read_text())
    assert not value['enabled'] and value['status']=='needs_user_action'
    assert not checks and not native.deliveries
    assert 'private access detail' not in target.read_text()
