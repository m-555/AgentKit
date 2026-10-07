"""Linux end of the WSL transport: the launch configuration, and the worker that runs Linux Claude.

A host starts the worker with `WslConfig.host_argv()`. Its first stdin line is the hello (key,
Claude argv, cwd, prompt), validated before Claude exists. Claude stdout lines go out unchanged,
and only a line that could pose as a control frame is wrapped, so model output cannot forge one.
Hook and MCP requests arrive on a private Unix socket, from this user and the Claude process tree
only, and each waits, bounded, for the host reply carrying its own id. Unknown, duplicate or
mismatched replies are dropped and reported. Claude leads its own process group, the only thing
ever signalled here, and the leader is reaped only after the group is signalled, so its id cannot
be reused in between. Host loss stops the group. The worker never opens AgentKit state and never
starts a Windows program.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .secrets import SECRET_NAME_HINTS
from .wsl_transport import (
    HELLO_ENV,
    SOCKET_ENV,
    TIMEOUTS,
    validate_argv,
)

#: What Claude inherits from the worker. Everything else, credentials above all, is dropped.
INHERITED = frozenset({
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "WSL_DISTRO_NAME",
    "CLAUDE_CONFIG_DIR",
})
MAX_CLIENTS = 16
HOST_LOSS_GRACE = 5.0
_DISTRO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_WINDOWS_PROGRAMS = (".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs")


def linux_path(value: object, what: str) -> str:
    """An absolute, normalised POSIX path with nothing whose meaning differs on Windows."""
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096:
        raise ValueError(f"{what} must be an absolute Linux path")
    if "\\" in value or any(ord(c) < 32 or c == "\x7f" for c in value):
        raise ValueError(f"{what} contains a backslash or a control character")
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise ValueError(f"{what} must be normalised, without empty, dot or dot-dot segments")
    return value


def linux_executable(value: object, what: str) -> str:
    """A Linux program. Windows-drive paths and Windows programs are refused outright."""
    path = linux_path(value, what)
    if path == "/mnt" or path.startswith("/mnt/") or path.casefold().endswith(_WINDOWS_PROGRAMS):
        raise ValueError(f"{what} must be a Linux executable outside /mnt, never a Windows program")
    return path


@dataclass(frozen=True)
class WslConfig:
    """Which Linux runtime a host launches: configured values, never measured ones."""

    distro: str
    user: str
    python: str
    claude: str
    wsl_exe: str = "wsl.exe"

    def __post_init__(self) -> None:
        if not isinstance(self.distro, str) or not _DISTRO.fullmatch(self.distro):
            raise ValueError("distro is not a plain WSL distribution name")
        if not isinstance(self.user, str) or not _USER.fullmatch(self.user):
            raise ValueError("user is not a plain Linux account name")
        linux_executable(self.python, "python")
        linux_executable(self.claude, "claude")
        if str(self.wsl_exe).replace("\\", "/").rsplit("/", 1)[-1].casefold() != "wsl.exe":
            raise ValueError("wsl_exe must name wsl.exe")

    def host_argv(self) -> list[str]:
        """Fixed tokens only. Claude argv, prompt and key travel in the hello, never on this line."""
        return [self.wsl_exe, "--distribution", self.distro, "--user", self.user, "--cd", "/",
                "--exec", self.python, "-I", "-m", "agentkit.wsl_worker"]

    def claude_argv(self, args: list[str]) -> list[str]:
        """The argv a hello carries, which always starts with the configured Claude."""
        if not isinstance(args, list):
            raise ValueError("Claude arguments must be a list")
        return validate_argv([self.claude, *args])


def _clamp(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        raise ValueError("timeouts must be numbers")
    return max(low, min(float(value), high))


@dataclass(frozen=True)
class Launch:
    """The hello after validation, built before Claude exists."""

    argv: list[str]
    cwd: str
    env: dict[str, str]
    prompt: str | None
    timeouts: dict[str, float]

    @classmethod
    def from_hello(cls, body: dict[str, Any]) -> Launch:
        argv = validate_argv(body.get("argv"))
        linux_executable(argv[0], "claude")
        cwd = linux_path(body.get("cwd"), "cwd")
        env, prompt, asked = body.get("env") or {}, body.get("prompt"), body.get("timeouts") or {}
        if not os.path.isdir(cwd):
            raise ValueError("cwd is not a directory")
        if not isinstance(env, dict) or not all(
                k in HELLO_ENV and isinstance(v, str) and "\x00" not in v for k, v in env.items()):
            raise ValueError(f"hello env may only set {sorted(HELLO_ENV)}")
        if not (prompt is None or isinstance(prompt, str)) or not isinstance(asked, dict):
            raise ValueError("prompt must be text and timeouts an object")
        timeouts = {op: _clamp(asked.get(op, limit), 0.1, limit) for op, limit in TIMEOUTS.items()}
        return cls(argv, cwd, dict(env), prompt, timeouts)


def child_environment(source: dict[str, str], extra: dict[str, str], socket_path: str) -> dict[str, str]:
    """An allowlisted Linux environment without credential variables. Windows PATH entries are
    dropped, so no Windows program is found by name."""
    env = {key: value for key, value in source.items()
           if key in INHERITED and not any(hint in key.upper() for hint in SECRET_NAME_HINTS)}
    entries = [p for p in source.get("PATH", "").split(":") if p.startswith("/") and not (p + "/").startswith("/mnt/")]
    env["PATH"] = ":".join(entries) or "/usr/local/bin:/usr/bin:/bin"
    return {**env, **extra, SOCKET_ENV: socket_path}


def _stat(pid: int) -> list[str]:
    with open(f"/proc/{pid}/stat", "rb") as stream:
        text = stream.read().decode("ascii", "replace")
    return text[text.rindex(")") + 2:].split()


def starttime(pid: int) -> int:
    """Clock ticks after boot when `pid` started. With the boot id it names one process for ever."""
    return int(_stat(pid)[19])


def boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        return stream.read().strip()


def descends_from(pid: int, ancestor: int) -> bool:
    """True when `ancestor` is `pid` or one of its parents. An unreadable chain is refused."""
    for _ in range(64):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        try:
            pid = int(_stat(pid)[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def measure(claude: str) -> dict[str, Any]:
    """What actually runs, reported apart from what was configured. It claims no capability."""
    import pwd
    real, digest, sha256, interop = os.path.realpath(claude), hashlib.sha256(), "", "absent or unreadable"
    with suppress(OSError), open(real, "rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
        sha256 = digest.hexdigest()
    with suppress(OSError), open("/proc/sys/fs/binfmt_misc/WSLInterop", encoding="ascii") as stream:
        interop = stream.readline().strip() or "unknown"
    return {"python": os.path.realpath(sys.executable), "python_version": platform.python_version(),
            "claude": real, "claude_sha256": sha256, "kernel": platform.release(), "interop": interop,
            "distro": os.environ.get("WSL_DISTRO_NAME", ""), "user": pwd.getpwuid(os.getuid()).pw_name}  # type: ignore[attr-defined]  # Linux-only runtime.


def _exit_status(pid: int) -> int | None:
    """Exit code of an exited child, or None while it runs. Never reaps, so the pid stays reserved."""
    try:
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # type: ignore[attr-defined]  # Linux-only runtime.
    except ChildProcessError:
        return -1
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status  # type: ignore[attr-defined]  # Linux-only runtime.


def _signal_group(pgid: int, signum: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)  # type: ignore[attr-defined]  # Linux-only runtime.


def _listen(path: str) -> socket.socket:
    """User-only: a 0700 directory from mkdtemp, and a 0600 socket bound under umask 177."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]  # Linux-only runtime.
    previous = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    server.listen(MAX_CLIENTS)
    server.settimeout(0.25)
    return server


def _cleanup(server: socket.socket | None, path: str, folder: str) -> None:
    if server is not None:
        server.close()
    with suppress(OSError):
        os.unlink(path)
    with suppress(OSError):
        os.rmdir(folder)


