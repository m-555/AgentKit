"""`.ai/tasks.yaml` — the committed task specification.

Authority split (PLAN_V3 §4.1): this file owns the *desired* graph — what work
exists, what it may touch, what it depends on. It deliberately has **no status
field**. Runtime facts (status, leases, attempts, spend, heartbeats) live only in
SQLite, so the two can never disagree about the same thing.

`spec_hash` is what makes edits detectable: change a task here while it is in
flight and reconcile moves it to NEEDS_REPLAN rather than silently running a
worker against a spec nobody approved.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ai_dir

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class TaskSpec:
    spec_id: str
    title: str
    description: str = ""
    kind: str = "SAFE_PARALLEL"
    role: str = "implementer"
    expected_write: list[str] = field(default_factory=list)
    expected_read: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    gate_level: str = "fast"
    priority: int = 100
    contract_version: int | None = None
    budget_usd: float | None = None
    job_id: str | None = None
    skills: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    complexity: str = "standard"
    model_profile: str | None = None
    model_assignment: str | dict | None = None

    def validate(self) -> None:
        from .db import TASK_KINDS
        if self.kind not in TASK_KINDS or self.kind == "OPERATOR":
            raise ValueError(f"unknown or reserved task kind: {self.kind}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", self.spec_id):
            raise ValueError(f"invalid task id: {self.spec_id!r}")
        if not self.title.strip() or not self.role.strip():
            raise ValueError("task title and role are required")
        if self.role in ("coordinator", "reviewer"):
            raise ValueError("coordinator and reviewer are supervisor-controlled roles, not worker tasks")
        if self.kind in ("RESEARCH", "REVIEW") and self.expected_write:
            raise ValueError("read-only task kinds cannot declare write paths")
        for path in [*self.expected_read, *self.expected_write, *self.skills]:
            if not path or path.startswith(("/", "\\")) or ":" in path or ".." in path.replace("\\", "/").split("/"):
                raise ValueError(f"task paths must stay within the repository: {path!r}")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd must be positive")
        if self.complexity not in ("easy", "standard", "complex"):
            raise ValueError("complexity must be easy, standard or complex")
        if self.model_profile not in (None, "sol", "opus", "sonnet", "qwen"):
            raise ValueError("invalid worker model_profile")
        if self.model_profile in ("sonnet", "qwen") and (self.complexity != "easy" or self.kind in ("HOTSPOT", "CONTRACT_CHANGE", "DECOUPLE")):
            raise ValueError("Sonnet/Qwen require easy tasks without shared-contract or architectural changes")
        if self.model_assignment is not None:
            from .policy import validate_task_shape
            if self.model_profile:
                raise ValueError("use model_assignment or model_profile, not both")
            validate_task_shape(self.model_assignment)

    def hash(self) -> str:
        """Stable over field order, sensitive to anything that changes the work."""
        payload = json.dumps(
            {
                "title": self.title,
                "description": self.description,
                "kind": self.kind,
                "role": self.role,
                "expected_write": sorted(self.expected_write),
                "expected_read": sorted(self.expected_read),
                "depends_on": sorted(self.depends_on),
                "gate_level": self.gate_level,
                "contract_version": self.contract_version,
                "priority": self.priority,
                "budget_usd": self.budget_usd,
                "job_id": self.job_id,
                "skills": self.skills,
                "acceptance": self.acceptance,
                "complexity": self.complexity, "model_profile": self.model_profile,
                **({"model_assignment": self.model_assignment} if self.model_assignment is not None else {}),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.spec_id,
            "title": self.title,
            "kind": self.kind,
            "role": self.role,
        }
        if self.description:
            data["description"] = self.description
        if self.expected_write or self.expected_read:
            data["expected_paths"] = {}
            if self.expected_write:
                data["expected_paths"]["write"] = list(self.expected_write)
            if self.expected_read:
                data["expected_paths"]["read"] = list(self.expected_read)
        if self.depends_on:
            data["depends_on"] = list(self.depends_on)
        if self.gate_level != "fast":
            data["gate_level"] = self.gate_level
        if self.priority != 100:
            data["priority"] = self.priority
        if self.contract_version is not None:
            data["contract_version"] = self.contract_version
        if self.budget_usd is not None:
            data["budget_usd"] = self.budget_usd
        for key in ("job_id", "skills", "acceptance", "complexity", "model_profile", "model_assignment"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> TaskSpec:
        allowed = {"id", "title", "description", "kind", "role", "expected_paths", "depends_on",
                   "gate_level", "priority", "contract_version", "budget_usd", "job_id", "skills", "acceptance", "complexity", "model_profile",
                   "model_assignment"}
        if set(data) - allowed:
            raise ValueError(f"unknown task fields: {sorted(set(data) - allowed)}")
        paths = data.get("expected_paths") or {}
        if not isinstance(paths, dict):
            raise ValueError("expected_paths must be a mapping")
        if set(paths) - {"write", "read"}:
            raise ValueError("expected_paths supports only read and write")
        for key, value in [("write", paths.get("write", [])), ("read", paths.get("read", [])),
                           *[(k, data.get(k, [])) for k in ("depends_on", "skills", "acceptance")]]:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of strings")
        title = str(data.get("title") or f"task-{index}")
        result = cls(
            spec_id=str(data.get("id") or slugify(title)),
            title=title,
            description=str(data.get("description") or ""),
            kind=str(data.get("kind") or "SAFE_PARALLEL"),
            role=str(data.get("role") or "implementer"),
            expected_write=[str(p) for p in (paths.get("write") or [])],
            expected_read=[str(p) for p in (paths.get("read") or [])],
            depends_on=[str(d) for d in (data.get("depends_on") or [])],
            gate_level=str(data.get("gate_level") or "fast"),
            priority=int(data.get("priority", 100)),
            contract_version=(
                int(data["contract_version"]) if data.get("contract_version") is not None else None
            ),
            budget_usd=(float(data["budget_usd"]) if data.get("budget_usd") is not None else None),
            job_id=data.get("job_id"), skills=list(data.get("skills", [])),
            acceptance=list(data.get("acceptance", [])),
            complexity=data.get("complexity", "standard"), model_profile=data.get("model_profile"),
            model_assignment=data.get("model_assignment"),
        )
        result.validate()
        return result


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:48] or "task"


def spec_path(root: str | Path) -> Path:
    return ai_dir(Path(root)) / "tasks.yaml"


def load(root: str | Path) -> list[TaskSpec]:
    path = spec_path(root)
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    entries = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: tasks must be a list")
    if not all(isinstance(item, dict) for item in entries):
        raise ValueError(f"{path}: every task must be a mapping")
    specs = [TaskSpec.from_dict(item, i) for i, item in enumerate(entries)]
    _check_unique(specs, path)
    _check_dependencies(specs, path)
    return specs


def _check_unique(specs: list[TaskSpec], path: Path) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.spec_id in seen:
            raise ValueError(f"{path}: duplicate task id {spec.spec_id!r}")
        seen.add(spec.spec_id)


def _check_dependencies(specs: list[TaskSpec], path: Path) -> None:
    ids = {s.spec_id for s in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in ids:
                raise ValueError(
                    f"{path}: task {spec.spec_id!r} depends on {dep!r}, which is not defined"
                )
    cycle = find_cycle(specs)
    if cycle:
        raise ValueError(f"{path}: dependency cycle: {' -> '.join(cycle)}")


def find_cycle(specs: list[TaskSpec]) -> list[str] | None:
    graph = {s.spec_id: list(s.depends_on) for s in specs}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        if state.get(node) == 2:
            return None
        if state.get(node) == 1:
            start = stack.index(node)
            return [*stack[start:], node]
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, ()):
            found = visit(dep)
            if found:
                return found
        stack.pop()
        state[node] = 2
        return None

    for spec_id in graph:
        found = visit(spec_id)
        if found:
            return found
    return None


def save(root: str | Path, specs: list[TaskSpec]) -> Path:
    from .locking import atomic_write
    path = spec_path(root)
    for task in specs:
        task.validate()
    _check_unique(specs, path)
    _check_dependencies(specs, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Task specification — the desired graph, committed to git.\n"
        "# Runtime state (status, leases, attempts, spend) lives in .ai/tasks.db and\n"
        "# never appears here. Editing a task that is in flight moves it to\n"
        "# NEEDS_REPLAN on the next `agentkit reconcile`.\n"
    )
    body = yaml.safe_dump(
        {"tasks": [s.to_dict() for s in specs]}, sort_keys=False, allow_unicode=True
    )
    atomic_write(path, header + body)
    return path


def topological_order(specs: list[TaskSpec]) -> list[TaskSpec]:
    """Dependency order; raises if the graph has a cycle."""
    cycle = find_cycle(specs)
    if cycle:
        raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")
    by_id = {s.spec_id: s for s in specs}
    ordered: list[TaskSpec] = []
    seen: set[str] = set()

    def visit(spec: TaskSpec) -> None:
        if spec.spec_id in seen:
            return
        seen.add(spec.spec_id)
        for dep in spec.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        ordered.append(spec)

    for spec in sorted(specs, key=lambda s: (s.priority, s.spec_id)):
        visit(spec)
    return ordered
