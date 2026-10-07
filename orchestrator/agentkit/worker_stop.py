"""Stop only the worker launched by this monitor, including its POSIX group."""
from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress


def stop(child, *, force=False):
    if os.name != "nt":
        # runner starts the child in a new session; the process group belongs to
        # this one launch. Children holding stdout open must also be stopped.
        with suppress(ProcessLookupError):
            killpg = os.killpg  # type: ignore[attr-defined]  # POSIX-only runtime branch.
            killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)  # type: ignore[attr-defined]
    elif child.poll() is None:
        if force:
            # PID refers to our live Popen handle, never a database PID guess.
            result = subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode and child.poll() is None:
                child.kill()
        else:
            child.terminate()
