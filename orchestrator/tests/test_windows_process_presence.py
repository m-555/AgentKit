"""A complete OS process list can prove absence; inaccessible handles cannot."""
import ctypes
import sys
from types import SimpleNamespace

import pytest

from agentkit import reconcile


@pytest.mark.parametrize("present,expected", [(False, False), (True, True), (None, True)])
def test_denied_handle_uses_complete_pid_snapshot(monkeypatch, present, expected):
    kernel = SimpleNamespace(OpenProcess=lambda *args: 0,
        GetExitCodeProcess=lambda *args: 0, CloseHandle=lambda *args: None)
    monkeypatch.setattr(reconcile, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setitem(sys.modules, "agentkit.windows_processes", SimpleNamespace(present=lambda pid: present))
    assert reconcile.pid_alive(20360) is expected


def test_enumeration_retries_full_buffer_and_preserves_protected_pids():
    from agentkit.windows_processes import present
    calls = []
    def enum(array, size, needed):
        calls.append(size)
        if len(calls) == 1:
            needed._obj.value = size
        else:
            array[0], array[1] = 7, 20360
            needed._obj.value = 8
        return 1
    assert present(20360, enum=enum) is True
    assert calls[1] > calls[0]


def test_complete_empty_snapshot_proves_absence():
    from agentkit.windows_processes import present
    def enum(array, size, needed):
        array[0] = 7
        needed._obj.value = 4
        return 1
    assert present(20360, enum=enum) is False


@pytest.mark.parametrize("mode", ["failure", "full", "malformed"])
def test_failed_truncated_or_malformed_enumeration_remains_unknown(mode):
    from agentkit.windows_processes import present
    def enum(array, size, needed):
        needed._obj.value = size if mode == "full" else 3
        return 0 if mode == "failure" else 1
    assert present(20360, enum=enum) is None
