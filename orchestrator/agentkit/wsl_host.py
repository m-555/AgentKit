"""Windows relay owns Linux process lifetime and all task authority."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .locking import atomic_write
from .wsl_dispatch import Dispatcher
from .wsl_runtime import WslConfig
from .wsl_session import HostSession
from .wsl_transport import HELLO_ENV


def write_native(line: str) -> None:
    """Relay UTF-8 JSON without the Windows pipe locale changing its bytes."""
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def run(config: dict, argv: list[str], cwd: str, prompt: str, *, expected_sha: str = "") -> int:
    runtime = WslConfig(**config)
    child = subprocess.Popen(runtime.host_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=sys.stderr.buffer, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.stdin and child.stdout
    dispatcher = Dispatcher()
    session = HostSession(child.stdin, child.stdout, on_native=write_native, serve=dispatcher.handle)
    state = None
    try:
        identity = session.start(argv, cwd, prompt=prompt, env={k: os.environ[k] for k in HELLO_ENV if k in os.environ})
        if expected_sha and identity.get("runtime", {}).get("claude_sha256") != expected_sha:
            raise ValueError("Linux Claude identity differs from the measured installation")
        root, process = os.environ.get("AGENTKIT_ROOT"), os.environ.get("AGENTKIT_PROCESS")
        if root and process:
            state = Path(root) / ".ai/runtime" / f"process-{int(process)}" / "transport.json"
            atomic_write(state, json.dumps({"config": config, "identity": identity, "handle": asdict(session.handle(child.pid)), "stopped": False}))
        # If the monitor closes its input, close the host channel: Linux stops its group.
        while not session.closed.wait(0.25):
            if session.compromised:
                raise RuntimeError("Authenticated transport integrity failure")
        code = session.exit_code
        if code is None:
            raise RuntimeError("Linux worker exited without confirming process-group termination")
        wrapper_code = child.wait(timeout=15)
        expected_code = code if code >= 0 else 128 - code
        if wrapper_code != expected_code:
            raise RuntimeError(f"WSL wrapper exit {wrapper_code} differs from worker exit {expected_code}")
        if state:
            atomic_write(state, json.dumps({"config": config, "identity": identity, "stopped": True, "exit": code}))
        return code if code >= 0 else 128 - code
    finally:
        try:
            if session.identity and not session.closed.is_set():
                session.terminate(grace=5)
        finally:
            session.close()
            dispatcher.close()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def confirmed_stopped(root, process_id) -> bool:
    """A dead Windows PID alone cannot authorize a competing WSL continuation."""
    path = Path(root) / ".ai/runtime" / f"process-{process_id}" / "transport.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        runtime = WslConfig(**data["config"])
        identity = data["identity"]
        code = ("import json,os,sys; from agentkit.wsl_runtime import boot_id,starttime; "
                "x=json.loads(sys.argv[1]); p=x['claude']['pid']; "
                "same=boot_id()==x['worker']['boot_id'] and os.path.exists('/proc/'+str(p)) and starttime(p)==x['claude']['starttime']; "
                "sys.exit(1 if same else 0)")
        args = [*runtime.host_argv()[:8], runtime.python, "-I", "-c", code, json.dumps(identity)]
        result = subprocess.run(args, capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def main() -> int:
    try:
        envelope = json.loads(sys.argv[1])
        return run(envelope["config"], envelope["argv"], envelope["cwd"], sys.stdin.read(), expected_sha=envelope["sha256"])
    except Exception as exc:
        sys.stderr.write(f"[AgentKit] WSL transport failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
