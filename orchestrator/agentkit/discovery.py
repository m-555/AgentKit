"""Finding the newest compatible Claude Code CLI without touching the user's PATH.

A VS Code extension often ships a newer native CLI than the one on PATH. Picking
the PATH copy silently runs an older client, and an older client may not accept a
newer model at all. So every discovered copy is version-checked and the highest
version wins. An explicit pin in `AGENTKIT_CLAUDE_CLI` is honoured exactly: if it
is too old for the requested model, launching fails with an actionable message
rather than quietly switching to another binary or another model.

Minimum versions come from the provider's published requirements and are listed
only where one is documented.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PIN_ENV = "AGENTKIT_CLAUDE_CLI"
EXTENSION_GLOB = "anthropic.claude-code-*"
INNER = ("resources/native-binary/claude.exe", "resources/native-binary/claude")

#: Oldest Claude Code CLI documented to support each model.
MIN_VERSION: dict[str, tuple[int, ...]] = {"claude-opus-5-5": (2, 1, 280)}

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_cache: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str            # configured | path | extension
    version: str           # "unknown" when the binary did not report one
    exists: bool = True

    @property
    def parsed(self) -> tuple[int, ...] | None:
        return parse_version(self.version)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def render(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


# Seams for tests: discovery must be simulated, never run against a live provider.
def _which(name: str) -> str | None:
    return shutil.which(name)


def _extension_dirs() -> list[Path]:
    from .adapters.base import EXTENSION_ROOTS
    return [p for p in (Path(r).expanduser() for r in EXTENSION_ROOTS) if p.is_dir()]


def _read_version(path: str) -> str:
    from .adapters.base import BaseAdapter
    raw = BaseAdapter._run_version(path, ["--version"])
    return raw.split()[0] if raw else "unknown"


def version_of(path: str) -> str:
    """`--version`, cached per binary identity so detection stays cheap."""
    try:
        stat = os.stat(path)
    except OSError:
        return "unknown"
    key = (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _cache:
        _cache[key] = _read_version(path)
    return _cache[key]


def candidates() -> list[Candidate]:
    pinned = os.environ.get(PIN_ENV, "").strip()
    if pinned:
        exists = Path(pinned).is_file()
        return [Candidate(pinned, "configured", version_of(pinned) if exists else "unknown", exists)]
    found: list[Candidate] = []
    seen: set[str] = set()

    def add(path: str, source: str) -> None:
        key = os.path.normcase(str(Path(path).resolve()))
        if key not in seen:
            seen.add(key)
            found.append(Candidate(path, source, version_of(path)))

    on_path = _which("claude")
    if on_path:
        add(on_path, "path")
    for base in _extension_dirs():
        for extension in base.glob(EXTENSION_GLOB):
            for inner in INNER:
                binary = extension / inner
                if binary.is_file():
                    add(str(binary), "extension")
                    break
    return found


def best(found: list[Candidate]) -> Candidate | None:
    """Highest parsed version; PATH wins a tie, unknown versions rank last."""
    usable = [c for c in found if c.exists]
    if not usable:
        return found[0] if found else None
    return max(usable, key=lambda c: (c.parsed is not None, c.parsed or (), c.source == "path"))


def problem(found: list[Candidate], selected: Candidate | None, model: str) -> str | None:
    """An actionable reason this CLI cannot run `model`, or None when it can."""
    required = MIN_VERSION.get(model)
    if selected is None:
        return None
    if not selected.exists and selected.source == "configured":
        return (f"{PIN_ENV} points to {selected.path}, which does not exist. Set it to a Claude Code "
                f"executable or unset it to use discovery.")
    if required is None:
        return None
    current = selected.parsed
    if current is not None and current >= required:
        return None
    others = ", ".join(f"{c.version} ({c.source}: {c.path})" for c in found if c is not selected) or "none"
    reported = selected.version if current is not None else "an unreadable version"
    hint = (f"unset {PIN_ENV} or point it at Claude Code {render(required)} or newer"
            if selected.source == "configured" else
            f"update Claude Code, or set {PIN_ENV} to a Claude Code {render(required)}+ executable "
            "(the VS Code extension ships one under resources/native-binary)")
    return (f"{model} requires Claude Code CLI {render(required)} or newer, but the selected CLI at "
            f"{selected.path} ({selected.source}) reports {reported}. Other installations found: {others}. "
            f"To fix: {hint}. AgentKit does not switch to another model or binary automatically.")


def install_problem(install: Any, model: str) -> str | None:
    """`problem` for an already-detected `Installation`."""
    exists = install.source != "configured" or Path(install.path).is_file()
    selected = Candidate(install.path, install.source, install.version, exists)
    return problem([selected], selected, model)


def report(model: str | None = None) -> dict[str, Any]:
    found = candidates()
    selected = best(found)
    return {"pin_env": PIN_ENV, "pinned": os.environ.get(PIN_ENV) or None,
            "selected": selected.to_dict() if selected else None,
            "candidates": [c.to_dict() for c in found],
            "requirements": {m: render(v) for m, v in MIN_VERSION.items()},
            "problems": {m: problem(found, selected, m) for m in ([model] if model else MIN_VERSION)}}
