"""Adapter registry.

Core imports this module and nothing deeper. Adding a vendor means adding a file
here and registering it — no core file changes, which is invariant 7 made
mechanical.
"""

from __future__ import annotations

from .base import AgentAdapter, AgentEvent, GuardReport, Installation, Launch
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .local_opencode import LocalOpenCodeAdapter

_REGISTRY: dict[str, AgentAdapter] = {}

#: Short names a person naturally types, mapped to the adapter that owns them.
#:
#: Resolution lives here rather than in the CLI so that every entry point agrees:
#: `--agent claude`, `adapter: claude` in `tasks.yaml` and a stored `adapter`
#: column all reach the same object. Keeping two spellings in two places is what
#: made `agentkit launch` unusable — its default was a name no adapter answered to.
ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
}


def register(adapter: AgentAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


register(ClaudeCodeAdapter())
register(CodexAdapter())
register(LocalOpenCodeAdapter())


def canonical_name(name: str) -> str:
    """The registry key for whatever spelling the caller used."""
    key = str(name or "").strip().lower()
    return ALIASES.get(key, key)


def get(name: str, project=None) -> AgentAdapter | None:
    canonical = canonical_name(name)
    settings = (getattr(project, "raw", {}) or {}).get("transports", {}).get(canonical)
    if settings:
        import os
        if canonical != "claude-code" or settings.get("type") != "wsl" or os.name != "nt":
            raise ValueError("Configured transport requires a Windows host and Claude WSL worker")
        from .wsl_claude import WslClaudeAdapter
        return WslClaudeAdapter(settings)
    return _REGISTRY.get(canonical)


def selectable_names() -> list[str]:
    """Every spelling `--agent` accepts: real names first, then aliases."""
    return [*sorted(_REGISTRY), *sorted(ALIASES)]


def all_adapters() -> list[AgentAdapter]:
    return list(_REGISTRY.values())


def installed(project=None) -> list[tuple[AgentAdapter, Installation]]:
    """Every adapter whose binary is actually present."""
    found = []
    for name in _REGISTRY:
        adapter = get(name, project)
        assert adapter is not None
        install = adapter.detect()
        if install is not None:
            found.append((adapter, install))
    return found


__all__ = [
    "AgentAdapter",
    "AgentEvent",
    "GuardReport",
    "Installation",
    "Launch",
    "all_adapters",
    "canonical_name",
    "get",
    "installed",
    "register",
    "selectable_names",
]
