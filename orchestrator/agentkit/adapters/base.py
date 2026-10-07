"""The adapter contract — the only place a vendor may be named.

Invariant 7: core names no vendor. Everything vendor-specific (binaries, flags,
hook file formats, event shapes) lives behind this interface, so a CLI changing
its flags, or disappearing, touches exactly one file.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

from ..capabilities import CapabilitySet

EXTENSION_ROOTS = (
    "~/.vscode/extensions",
    "~/.vscode-insiders/extensions",
    "~/.cursor/extensions",
    "~/.windsurf/extensions",
)

#: Enforcement layers from PLAN_V3 §2.2, as reported by `install_guards`.
GUARD_LAYERS = (
    "L0_worktree",
    "L1_sandbox",
    "L2_permissions",
    "L3_prewrite_guard",
    "L4_shell_guard",
)


@dataclass
class Installation:
    """A located agent binary."""

    name: str
    path: str
    version: str
    source: str = "path"                     # "path" | "extension" | "configured"
    identity: dict[str, str] | None = None

    def summary(self) -> str:
        return f"{self.name} {self.version} ({self.source}: {self.path})"


@dataclass
class Launch:
    """A fully-formed command to start a worker. Core never builds one itself."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "."
    stdin_text: str | None = None

    def display(self) -> str:
        env_bits = " ".join(f"{k}={v}" for k, v in sorted(self.env.items()))
        argv = " ".join(f'"{a}"' if " " in a else a for a in self.argv)
        return f"[cwd={self.cwd}] {env_bits}\n  {argv}" + (f"\n[stdin: {len(self.stdin_text)} characters]" if self.stdin_text else "")


@dataclass
class GuardReport:
    """Which enforcement layers are actually live for this worker.

    Reported rather than assumed: the scheduler refuses HOTSPOT work when the
    report shows no pre-write guard, which is how §3's capability model reaches
    the point of scheduling.
    """

    active: set[str] = field(default_factory=set)
    inactive: dict[str, str] = field(default_factory=dict)   # layer -> why not
    files_written: list[str] = field(default_factory=list)

    def has(self, layer: str) -> bool:
        return layer in self.active

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": sorted(self.active),
            "inactive": dict(self.inactive),
            "files_written": list(self.files_written),
        }


@dataclass
class AgentEvent:
    """A vendor event normalised into core's vocabulary."""

    kind: str          # started | tool_use | text | commit | error | cost | finished
    at: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@runtime_checkable
class AgentAdapter(Protocol):
    def check_availability(self) -> dict: ...

    name: str

    def detect(self) -> Installation | None: ...

    def default_capabilities(self) -> CapabilitySet: ...

    def build_launch(
        self, task: dict[str, Any], worktree: Path, role: str, project: Any,
        *, prompt: str, resume_token: str | None = None,
    ) -> Launch: ...

    def install_guards(
        self, worktree: Path, task: dict[str, Any], orchestrator: Path
    ) -> GuardReport: ...

    def parse_events(self, stream: IO[str]) -> Iterator[AgentEvent]: ...

    def classify_error(self, text: str, exit_code: int | None) -> Any:
        """Map this vendor's failure output onto core's error classes."""
        ...

    def account_id(self) -> str:
        """Which login this adapter uses. Workers sharing it share its allowance."""
        ...

    def runtime_problem(self, model: str) -> str | None:
        """Why the installed runtime cannot run `model`, or None. Never a fallback."""
        ...


class BaseAdapter:
    """Shared plumbing. Subclasses stay small and vendor-shaped."""

    name = "base"

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def _extension_dirs() -> list[Path]:
        return [p for p in (Path(r).expanduser() for r in EXTENSION_ROOTS) if p.is_dir()]

    @classmethod
    def _find_in_extensions(cls, ext_glob: str, inner_globs: tuple[str, ...]) -> str | None:
        """Newest matching extension wins; versions sort lexically by suffix."""
        for base in cls._extension_dirs():
            for ext in sorted(base.glob(ext_glob), reverse=True):
                for inner in inner_globs:
                    for candidate in sorted(ext.glob(inner)):
                        if candidate.is_file():
                            return str(candidate)
        return None

    @staticmethod
    def _on_path(binary: str) -> str | None:
        return shutil.which(binary)

    @staticmethod
    def _run_version(path: str, args: list[str]) -> str:
        import subprocess

        try:
            proc = subprocess.run(
                [path, *args], capture_output=True, text=True, timeout=30,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (
            proc.stdout or proc.stderr
        ) else ""

    # -- defaults ----------------------------------------------------------

    def default_capabilities(self) -> CapabilitySet:
        """Optimistic starting point. `probe.py` demotes anything it cannot prove."""
        return CapabilitySet(adapter=self.name)

    def parse_events(self, stream: IO[str]) -> Iterator[AgentEvent]:
        for line in stream:
            line = line.strip()
            if line:
                yield AgentEvent(kind="text", raw=line)

    #: Vendor-specific failure wording. Checked before the generic patterns, so a
    #: provider can be precise about its own messages. Subclasses override.
    ERROR_PATTERNS: tuple[tuple[str, str, Any], ...] = ()

    #: Workers sharing one login share one allowance, and so share one cooldown.
    def account_id(self) -> str:
        return "default"

    def check_availability(self) -> dict:
        return {"available": None, "reason": "adapter has no availability transport", "windows": []}

    def runtime_problem(self, model: str) -> str | None:
        return None

    def classify_error(self, text: str, exit_code: int | None = None):
        from .. import errors

        return errors.with_retry_at(
            errors.classify(text, exit_code, extra_patterns=self.ERROR_PATTERNS)
        )
