"""A Windows relay exit cannot release an unconfirmed Linux worker's ownership."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .locking import atomic_write


def is_wsl(process) -> bool:
    return json.loads(process.get("launch_json") or "{}").get("env", {}).get("AGENTKIT_TRANSPORT") == "wsl"


def path(root, process_id):
    return Path(root) / ".ai/runtime" / f"process-{int(process_id)}" / "transport.json"


def exit_confirmed(root, process_id) -> bool:
    try:
        data = json.loads(path(root, process_id).read_text(encoding="utf-8"))
        return data.get("stopped") is True and isinstance(data.get("identity"), dict)
    except (OSError, ValueError):
        return False


def group_absent(data) -> bool:
    """A lost leader must not release ownership while its Linux children still run."""
    from .wsl_runtime import WslConfig
    runtime = WslConfig(**data["config"])
    code = """import json,sys
from pathlib import Path
x=json.loads(sys.argv[1])
if Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=x['worker']['boot_id']:
    sys.exit(0)
for path in Path('/proc').iterdir():
    if not path.name.isdigit(): continue
    try:
        text=(path/'stat').read_text()
    except FileNotFoundError:
        continue
    values=text[text.rindex(')')+2:].split()
    if int(values[2])==x['claude']['pgid'] and values[0] not in ('Z','X'):
        sys.exit(1)
sys.exit(0)
"""
    try:
        result = subprocess.run([*runtime.host_argv()[:8], runtime.python, "-I", "-c", code,
                                 json.dumps(data["identity"])], capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def recover_exit(root, process_id) -> bool:
    from .wsl_host import confirmed_stopped
    if not confirmed_stopped(root, process_id):
        return False
    target = path(root, process_id)
    data = json.loads(target.read_text(encoding="utf-8"))
    if not group_absent(data):
        return False
    data.update(stopped=True, recovery="Linux boot/PID/start identity and process group confirmed absent")
    atomic_write(target, json.dumps(data))
    return True
