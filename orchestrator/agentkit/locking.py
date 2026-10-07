"""Process locks released by the OS on exit, including after a crash."""
from __future__ import annotations

import importlib
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockBusy(TimeoutError):
    """Another process still owns an OS coordination lock."""


@contextmanager
def exclusive(root: str | Path, name: str, timeout: float = 30):
    path = Path(root) / ".ai" / "runtime" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"another process holds the {name} lock") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers/antivirus may briefly deny an atomic replacement.
                # Never change ACLs or replace non-atomically; preserve the old file.
                if getattr(exc, "winerror", None) not in (5, 32) or attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
