"""Bounded read-only OS PID enumeration; incomplete lists never prove absence."""
from __future__ import annotations

import ctypes


def present(pid, *, enum=None):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        word = ctypes.c_uint32
        if enum is None:
            enum = ctypes.WinDLL("psapi", use_last_error=True).EnumProcesses
            enum.argtypes = [ctypes.POINTER(word), word, ctypes.POINTER(word)]
            enum.restype = ctypes.c_int
        capacity = 1024
        for _ in range(8):
            array = (word * capacity)()
            size = ctypes.sizeof(array)
            needed = word()
            if not enum(array, size, ctypes.byref(needed)):
                return None
            if needed.value > size or needed.value % ctypes.sizeof(word):
                return None
            if needed.value < size:
                return pid in array[:needed.value // ctypes.sizeof(word)]
            capacity *= 2
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    return None
