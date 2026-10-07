"""Independent cadence, crash retry and singleton ownership use no model turns."""
import json
import os
from types import SimpleNamespace

import pytest

from agentkit import native_recovery_service as service
from agentkit.locking import LockBusy, exclusive


def test_database_failure_retries_then_observes_native_without_supervisor(tmp_path, monkeypatch):
    calls = []
    connection = SimpleNamespace(close=lambda: calls.append('close'))
    def connect(_root):
        calls.append('connect')
        if calls.count('connect') == 1:
            raise OSError('PRIVATE_FAILURE')
        return connection
    monkeypatch.setattr(service.db, 'connect', connect)
    monkeypatch.setattr(service.native_session_recovery, 'tick', lambda *_args: calls.append('native') or ['observed'])
    sleeps = []
    service.run(tmp_path, sleeper=sleeps.append, max_iterations=2)
    assert calls == ['connect', 'connect', 'native', 'close']
    assert sleeps == [20, 20]
    health = json.loads((tmp_path / '.ai/runtime/native-recovery-health.json').read_text())
    assert health['status'] == 'running' and health['notes'] == ['observed']
    assert 'PRIVATE_FAILURE' not in json.dumps(health)


def test_live_service_lock_prevents_a_competing_native_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(service.db, 'connect', lambda *_: pytest.fail('Must not compete'))
    with exclusive(tmp_path, 'native-recovery-service'):
        with pytest.raises(LockBusy):
            service.run(tmp_path, sleeper=lambda _: None, max_iterations=1)


def test_current_healthy_service_is_not_spawned_again(tmp_path, monkeypatch):
    for key in ('AGENTKIT_TASK', 'AGENTKIT_PROCESS'):
        monkeypatch.delenv(key, raising=False)
    target = tmp_path / '.ai/runtime/native-recovery-health.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'pid':os.getpid(),'at':service.db.utcnow()}))
    monkeypatch.setattr(service.process_identity, 'alive', lambda _: True)
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *_args, **_kw: pytest.fail('No duplicate'))
    assert service.start(tmp_path) == 'Native recovery service already running'


def test_dead_service_is_started_hidden_without_model_launch(tmp_path, monkeypatch):
    for key in ('AGENTKIT_TASK', 'AGENTKIT_PROCESS'):
        monkeypatch.delenv(key, raising=False)
    launches = []
    monkeypatch.setattr(service.process_identity, 'fingerprint', lambda _: {'born':'known'})
    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(service.subprocess, 'Popen', spawn)
    assert service.start(tmp_path) == 'Native recovery service started'
    assert 'agentkit.native_recovery_service' in launches[0][0]
    assert '--model' not in launches[0][0]
    assert launches[0][1]['stdin'] == service.subprocess.DEVNULL
    assert json.loads((tmp_path / '.ai/runtime/native-recovery-health.json').read_text())['status'] == 'starting'


def test_worker_cannot_start_host_service(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTKIT_TASK','7')
    with pytest.raises(PermissionError):
        service.start(tmp_path)
