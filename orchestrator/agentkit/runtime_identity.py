"""Executable identity attached to a measured confinement capability."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def identify(install) -> dict[str, str]:
    if getattr(install, "identity", None):
        return dict(install.identity)
    path = Path(install.path).resolve()
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return {"path": os.path.normcase(str(path)), "source": install.source, "sha256": ""}
    return {"path": os.path.normcase(str(path)), "source": install.source, "sha256": digest.hexdigest(), "enforcement": enforcement_digest(install.name)}


LEGACY_NAMES = ("hooks_cli.py", "hook_worktree_paths.py", "codex_hooks.py", "patch_paths.py",
                "leases.py", "shellguard.py", "adapters/codex.py", "codex_trust.py",
                "adapters/claude_code.py", "adapters/wsl_claude.py", "wsl_host.py",
                "wsl_client.py", "wsl_worker.py", "wsl_runtime.py", "wsl_transport.py",
                "wsl_dispatch.py", "wsl_session.py", "wsl_mcp_protocol.py", "wsl_mcp_stream.py")
CODEX_NAMES = {"codex_hooks.py", "patch_paths.py", "adapters/codex.py", "codex_trust.py"}
COMMON_NAMES = {"hooks_cli.py", "hook_worktree_paths.py", "leases.py", "shellguard.py"}


def enforcement_names(provider: str | None = None) -> tuple[str, ...]:
    # The WSL Claude adapter's historical no-argument call selects its own guards.
    if provider is None or provider == "claude-code":
        return tuple(name for name in LEGACY_NAMES if name not in CODEX_NAMES)
    if provider == "codex":
        return (*(name for name in LEGACY_NAMES if name in CODEX_NAMES | COMMON_NAMES), "guard_files.py")
    return LEGACY_NAMES  # Unknown adapters remain conservative.


def digest_sources(names, read) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        value = read(name)
        if value is not None:
            digest.update(value)
    digest.update(str(Path(__import__("sys").executable).resolve()).encode())
    return digest.hexdigest()


def enforcement_digest(provider: str | None = None) -> str:
    base = Path(__file__).parent
    return digest_sources(enforcement_names(provider),
                          lambda name: (base / name).read_bytes() if (base / name).is_file() else None)
