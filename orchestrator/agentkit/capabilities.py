"""What an agent installation can actually do.

Core reasons about capabilities, never about vendors. The names here are the only
vocabulary the scheduler understands; an adapter's job is to translate its
vendor's reality into this set, and `probe.py`'s job is to verify that the
translation is true rather than hopeful.

Invariant 8: capabilities are measured, never assumed. Nothing in this module
keys off a version string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Every capability core knows about. Adapters may not invent new names.
CAPABILITIES = (
    "prewrite_file_guard",   # can block a file write before it lands
    "shell_guard",           # can block a shell command before it runs
    "workspace_sandbox",     # OS-enforced confinement of writes to the workspace
    "network_control",       # domain allow/deny for outbound traffic
    "resume_session",        # can continue a prior session with its context
    "fork_session",          # can branch a prior session into a new one
    "structured_output",     # returns a typed result, not prose
    "mcp_stdio",             # can reach the AgentKit MCP server
    "worktree_native",       # creates its own git worktree
    "budget_cap",            # spend can be bounded per run
    "event_stream",          # emits incremental events for supervision
    "subagents",             # can delegate to sub-sessions
)

#: Task kinds whose worker writes source or test files unattended.
#:
#: These all need `write_worker_safe` (§2). Lease auditing alone cannot police a
#: worker that writes *into another worktree*: the victim's audit sees a change to
#: a path the victim legitimately owns, and the offender's own branch does not
#: contain it, so no amount of after-the-fact diffing can attribute the write.
#: The only fix is at the isolation layer — prove filesystem confinement before
#: granting unattended write work.
WRITE_TASK_KINDS = (
    "HOTSPOT", "DECOUPLE", "CONTRACT_CHANGE", "DEPENDENT", "SAFE_PARALLEL", "TEST_ONLY",
)

#: Kinds that never write source and so need no confinement proof.
READONLY_TASK_KINDS = ("RESEARCH", "REVIEW", "OPERATOR")

#: What each task kind needs before the scheduler will assign it.
REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "HOTSPOT": ("write_worker_safe", "strong_write_isolation",
                "structured_checkpointing", "recoverable"),
    "DECOUPLE": ("write_worker_safe", "strong_write_isolation",
                 "structured_checkpointing", "recoverable"),
    "CONTRACT_CHANGE": ("write_worker_safe", "strong_write_isolation",
                        "structured_checkpointing"),
    "DEPENDENT": ("write_worker_safe", "recoverable"),
    "SAFE_PARALLEL": ("write_worker_safe", "recoverable"),
    "TEST_ONLY": ("write_worker_safe", "recoverable"),
    "RESEARCH": (),
    "REVIEW": (),
    "OPERATOR": (),
}


@dataclass
class CapabilitySet:
    """Measured capabilities for one installation of one agent."""

    adapter: str
    version: str = ""
    probed_at: str = ""
    values: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    installation: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in CAPABILITIES:
            self.values.setdefault(name, False)

    def __getitem__(self, name: str) -> bool:
        if name not in CAPABILITIES and name not in self.derived():
            raise KeyError(f"unknown capability {name!r}")
        if name in CAPABILITIES:
            return bool(self.values.get(name, False))
        return self.derived()[name]

    def has(self, name: str) -> bool:
        try:
            return self[name]
        except KeyError:
            return False

    def set(self, name: str, value: bool, note: str = "") -> None:
        if name not in CAPABILITIES:
            raise KeyError(f"unknown capability {name!r}")
        self.values[name] = bool(value)
        if note:
            self.notes[name] = note

    def derived(self) -> dict[str, bool]:
        raw = self.values
        # Proven filesystem confinement. The mechanism is not named here on
        # purpose: an OS sandbox, a container, a restricted process token or a
        # separate OS account all satisfy it, and an adapter reports whichever
        # it genuinely has. What is *not* acceptable is an agent that merely
        # promises to stay put.
        confined = bool(raw.get("workspace_sandbox"))
        strong = confined and (
            bool(raw.get("prewrite_file_guard")) or bool(raw.get("shell_guard"))
        )
        structured = bool(raw.get("mcp_stdio")) and bool(raw.get("structured_output"))
        recoverable = bool(raw.get("resume_session")) or structured
        supervisable = bool(raw.get("event_stream")) or bool(raw.get("budget_cap"))
        return {
            "write_worker_safe": confined,
            "strong_write_isolation": strong,
            "structured_checkpointing": structured,
            "recoverable": recoverable,
            "supervisable": supervisable,
        }

    def missing_for(self, task_kind: str) -> list[str]:
        """Which requirements this installation fails for a kind of task."""
        if task_kind not in REQUIREMENTS:
            return [f"unknown task kind {task_kind}"]
        needed = REQUIREMENTS[task_kind]
        return [name for name in needed if not self.has(name)]

    def can_run(self, task_kind: str) -> bool:
        return not self.missing_for(task_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "version": self.version,
            "probed_at": self.probed_at,
            "capabilities": dict(self.values),
            "derived": self.derived(),
            "notes": dict(self.notes),
            "installation": dict(self.installation),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilitySet:
        return cls(
            adapter=str(data.get("adapter") or ""),
            version=str(data.get("version") or ""),
            probed_at=str(data.get("probed_at") or ""),
            values={k: bool(v) for k, v in (data.get("capabilities") or {}).items()
                    if k in CAPABILITIES},
            notes={str(k): str(v) for k, v in (data.get("notes") or {}).items()},
            installation={str(k): str(v) for k, v in (data.get("installation") or {}).items()},
        )

    def stamp(self) -> None:
        self.probed_at = datetime.now(UTC).isoformat(timespec="seconds")


def cache_path(root: str | Path) -> Path:
    return Path(root) / ".ai" / "capabilities.json"


def load_cache(root: str | Path) -> dict[str, CapabilitySet]:
    path = cache_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("adapters") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    result = {name: CapabilitySet.from_dict(spec) for name, spec in entries.items()}
    for caps in result.values():
        for name in ("workspace_sandbox", "prewrite_file_guard", "shell_guard"):
            note = caps.notes.get(name, "")
            if caps.has(name) and note.startswith("functional") and not note.startswith("functional v2:"):
                caps.set(name, False, "unverified: legacy probe evidence; fresh functional probe required")
    return result


def save_cache(root: str | Path, sets: dict[str, CapabilitySet]) -> None:
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"adapters": {name: cap.to_dict() for name, cap in sets.items()}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_stale(cached: CapabilitySet, current_version: str) -> bool:
    """A cached probe is stale as soon as the binary changes underneath it."""
    return cached.version != current_version


def cached_runtime_problem(root, adapter, kind: str) -> str | None:
    """Confinement measured for another executable cannot authorize writes."""
    if kind not in WRITE_TASK_KINDS:
        return None
    install = adapter.detect()
    if install is None:
        return None
    caps = load_cache(root).get(adapter.name)
    from .runtime_identity import identify
    identity = identify(install)
    if not caps or caps.version != install.version or not identity["sha256"] or caps.installation != identity:
        return f"{adapter.name} {install.version} requires a fresh functional confinement probe; cached runtime version differs"
    return None
