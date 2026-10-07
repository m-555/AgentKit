"""Service recovery is host code and never starts provider workers itself."""
import json
from types import SimpleNamespace

import pytest

from agentkit import service_guardian


@pytest.mark.parametrize("first", [1, 0, OSError("temporary launch failure")])
def test_crash_and_idle_restart_preserve_root_and_worker_cap(project_root, first):
    launches, sleeps = [], []
    codes = iter([first, 0])

    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        code = next(codes)
        if isinstance(code, OSError):
            raise code
        return SimpleNamespace(pid=123, wait=lambda: code)

    service_guardian.run(project_root, "2", spawn=spawn, sleeper=sleeps.append, max_iterations=2)
    assert len(launches) == 2
    assert all(item[0][-3:] == [str(project_root), "2", "--worker"] for item in launches)
    assert sleeps == ([40, 20] if first else [20, 20])
    health = json.loads((project_root / ".ai/runtime/supervisor-health.json").read_text())
    assert health["status"] == "idle" and health["iteration"] == 2
    assert health["exit_code"] == 0 and health["worker_cap"] == "2"


def test_repeated_host_failure_has_bounded_backoff_and_visible_health(project_root):
    delays = []

    def failed(*args, **kwargs):
        raise OSError("host cannot start supervisor")

    service_guardian.run(project_root, "config", spawn=failed, sleeper=delays.append, max_iterations=8)
    assert delays == [40, 80, 160, 300, 300, 300, 300, 300]
    health = json.loads((project_root / ".ai/runtime/supervisor-health.json").read_text())
    assert health["status"] == "retrying" and "cannot start" in health["error"]
