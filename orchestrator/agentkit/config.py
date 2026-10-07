"""Reading `.ai/project.yaml`.

This is the entire per-project configuration surface. Everything the
framework does differently between a Python service, a React app and an Unreal
plugin is expressed here rather than in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ai_dir


@dataclass
class ProjectConfig:
    root: Path
    name: str = ""
    stacks: list[str] = field(default_factory=list)
    gates: dict[str, list[str]] = field(default_factory=dict)
    hot_paths: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    budgets: dict[str, float] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def protected(self) -> list[str]:
        """Paths that always need an explicit lease, never incidental access."""
        return [*self.hot_paths, *self.contracts]

    @property
    def worktree_setup(self) -> list[str]:
        """Commands that build a worker's environment inside its fresh worktree.

        A worktree deliberately receives no copy of `.venv` or `node_modules` —
        sharing a writable environment lets one worker corrupt another's. The
        consequence is that a gate like `pytest` has nothing to run against until
        something builds an environment there, and without this the worker fails a
        gate it was never able to pass.
        """
        raw = self.raw.get("worktree_setup") or []
        if isinstance(raw, str):
            return [raw]
        return [str(c) for c in raw]

    def gate(self, level: str) -> list[str]:
        return list(self.gates.get(level, []))

    def model_for(self, role: str) -> str | None:
        return self.models.get(role)


def load_project(root: str | Path) -> ProjectConfig:
    root_path = Path(root)
    path = ai_dir(root_path) / "project.yaml"
    if not path.is_file():
        return ProjectConfig(root=root_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ProjectConfig(root=root_path)
    gates = data.get("gates") or {}
    if not isinstance(gates, dict):
        gates = {}
    normalised_gates = {
        str(k): ([v] if isinstance(v, str) else [str(i) for i in (v or [])])
        for k, v in gates.items()
    }
    return ProjectConfig(
        root=root_path,
        name=str(data.get("name") or root_path.name),
        stacks=[str(s) for s in (data.get("stacks") or [])],
        gates=normalised_gates,
        hot_paths=[str(p) for p in (data.get("hot_paths") or [])],
        contracts=[str(p) for p in (data.get("contracts") or [])],
        budgets={str(k): float(v) for k, v in (data.get("budgets") or {}).items()},
        models={str(k): str(v) for k, v in (data.get("models") or {}).items()},
        raw=data,
    )
