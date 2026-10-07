"""A competing finite supervisor pass must not kill the background service."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentkit import supervisor, watch
from agentkit.locking import exclusive


def test_watch_retries_real_supervisor_lock_without_scheduling(project_root, monkeypatch):
    scheduled = []
    sleeps = []

    @contextmanager
    def immediate(root, name, timeout=30):
        with exclusive(root, name, timeout=0):
            yield

    monkeypatch.setattr(supervisor, "exclusive", immediate)
    monkeypatch.setattr(watch.scheduler, "run_once", lambda *a, **kw: scheduled.append(1))
    with exclusive(project_root, "supervisor", timeout=0):
        result = watch._run(project_root, max_iterations=2, sleeper=sleeps.append)
    assert result.stopped_reason == "iteration limit reached"
    assert result.iterations == 2
    assert scheduled == []
    assert sleeps == [20, 20]
    assert any("supervisor lock" in line for line in result.history)


def test_watch_does_not_hide_non_lock_timeouts(project_root, monkeypatch):
    def failed(*args, **kwargs):
        raise TimeoutError("regression check timeout")
    monkeypatch.setattr(supervisor, "tick", failed)
    with pytest.raises(TimeoutError, match="regression check timeout"):
        watch._run(project_root, max_iterations=1, sleeper=lambda seconds: None)


def test_watch_resumes_normal_pass_after_contention(project_root, monkeypatch):
    from agentkit.locking import LockBusy
    calls = []
    scheduled = []
    def tick(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise LockBusy("another process holds the supervisor lock")
        return []
    monkeypatch.setattr(supervisor, "tick", tick)
    monkeypatch.setattr(watch.reconcile, "reconcile", lambda *a, **kw: None)
    monkeypatch.setattr(watch.scheduler, "unavailable_adapters", lambda conn: {})
    def run(*args, **kwargs):
        scheduled.append(1)
        return SimpleNamespace(launched=[], woken=[], summary=lambda: "idle")
    monkeypatch.setattr(watch.scheduler, "run_once", run)
    monkeypatch.setattr(watch, "idle_reason", lambda conn: None)
    result = watch._run(project_root, max_iterations=2, sleeper=lambda seconds: None)
    assert result.iterations == 2
    assert scheduled == [1]
