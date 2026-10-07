"""Independent host recovery cadence; long checks never block native heartbeats."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db, native_session_recovery, process_identity
from .locking import LockBusy, atomic_write, exclusive


def start(root):
    if os.environ.get('AGENTKIT_TASK') or os.environ.get('AGENTKIT_PROCESS'):
        raise PermissionError('Only the host/operator can start native recovery service')
    root = Path(root).resolve()
    runtime = root / '.ai/runtime'
    health = runtime / 'native-recovery-health.json'
    with exclusive(root, 'native-recovery-start'):
        try:
            previous = json.loads(health.read_text(encoding='utf-8'))
            stamp = db.parse_ts(previous.get('at'))
            if stamp and stamp + timedelta(seconds=120) > datetime.now(UTC) and process_identity.alive(previous):
                return 'Native recovery service already running'
        except (OSError, ValueError, TypeError):
            pass
        from .secrets import worker_environment
        with (runtime / 'native-recovery-service.log').open('a', encoding='utf-8') as log:
            child = subprocess.Popen([sys.executable, '-m', 'agentkit.native_recovery_service', str(root)],
                cwd=Path(__file__).resolve().parents[1], env=worker_environment(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
        # STARTING identity prevents duplicate starts before the child owns its lock.
        identity = process_identity.fingerprint(child.pid)
        atomic_write(health, json.dumps({'pid': child.pid, 'pid_identity': json.dumps(identity) if identity else None,
            'at': db.utcnow(), 'status': 'starting'}) + '\n')
        return 'Native recovery service started'


def run(root, *, sleeper=time.sleep, max_iterations=None):
    root = Path(root).resolve()
    health = root / '.ai/runtime/native-recovery-health.json'
    with exclusive(root, 'native-recovery-service', timeout=0):
        identity = process_identity.fingerprint(os.getpid())
        base = {'pid': os.getpid(), 'pid_identity': json.dumps(identity) if identity else None}
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            connection = None
            state = {**base, 'iteration': iteration, 'status': 'running'}
            try:
                connection = db.connect(root)
                state['notes'] = native_session_recovery.tick(connection, root)[-2:]
            except Exception as error:
                # Preserve the registered intent and keep this host loop alive.
                # Do not persist private IPC/error payloads or claim wake success.
                state.update(status='degraded_retrying', error=type(error).__name__)
            finally:
                if connection is not None:
                    connection.close()
                state.update(at=db.utcnow(), next_seconds=20)
                atomic_write(health, json.dumps(state) + '\n')
            sleeper(20)


if __name__ == '__main__':
    # Another host service may own the cadence; never compete with it.
    with suppress(LockBusy):
        run(Path(sys.argv[1]))
