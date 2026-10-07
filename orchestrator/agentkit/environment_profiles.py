"""Select and fingerprint the dependency tools actually needed by a gate."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import repo


def select(project, task=None, *, level=None, profile_name=None, _seen=()):
    level = level or (task or {}).get("gate_level", "full")
    profiles = project.raw.get("environment_profiles") or {}
    if not profiles:
        return {"name": "legacy", "setup": project.worktree_setup, "requires": [],
                "strategy": "private-cache", "min_free_bytes": 268435456,
                "reserve_bytes": 268435456, "inputs": []}
    if not isinstance(profiles, dict):
        raise ValueError("environment_profiles must be a mapping")
    name = profile_name or (project.raw.get("environment_gates") or {}).get(level)
    if not name:
        name = (project.raw.get("environment_roles") or {}).get((task or {}).get("role"))
    if not name or name not in profiles:
        raise ValueError(f"No environment profile covers gate {level}")
    if name in _seen:
        raise ValueError("Environment component cycle")
    value = profiles[name]
    if not isinstance(value, dict):
        raise ValueError("Environment profile must be a mapping")
    result = {"name": name, "setup": [], "requires": [], "inputs": [],
              "strategy": "private-cache", "min_free_bytes": 268435456,
              "reserve_bytes": 268435456, **value}
    if result["strategy"] not in ("private-cache", "snapshot-copy", "python-wheels"):
        raise ValueError("Writable environment sharing is unsupported; select private-cache, snapshot-copy or python-wheels")
    for field in ("setup", "requires", "inputs", "checks"):
        if not isinstance(result.get(field, []), list) or not all(isinstance(x, str) for x in result.get(field, [])):
            raise ValueError(f"Profile {field} must be a list of strings")
    for field in ("requires", "inputs"):
        for value in result[field]:
            if Path(value).is_absolute() or ".." in Path(value).parts or "\\" in value:
                raise ValueError("Environment paths must be relative slash-separated paths")
    for field in ("min_free_bytes", "reserve_bytes"):
        if type(result[field]) is not int or result[field] < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
    components = result.get("components", [])
    if not isinstance(components, list) or not all(isinstance(x, str) for x in components):
        raise ValueError("Environment components must name profiles")
    if components and result["setup"]:
        raise ValueError("Composite profiles use components rather than duplicate setup commands")
    result["component_profiles"] = {n: select(project, task, level=level, profile_name=n, _seen=(*_seen, name)) for n in components}
    declared = set(result.get("tools", []))
    commands = project.gate(level)
    needs = set()
    for command in commands:
        if re.search(r"\b(npm|npx|pnpm|yarn)\b", command):
            needs.add("javascript")
        if re.search(r"(?:venv|\.venv)[/\\].*python", command, re.I):
            needs.add("python-packages")
    if not profile_name and needs - declared:
        raise ValueError(f"Profile {name} does not cover gate tools: {sorted(needs - declared)}")
    return result


def inputs(work):
    work = Path(work)
    try:
        paths = repo.tracked_files(work)
    except (OSError, subprocess.SubprocessError):
        paths = []
    names = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "uv.lock", "poetry.lock", "Pipfile.lock", "pyproject.toml", ".npmrc"}
    paths = sorted(set(paths) | {p.name for p in work.iterdir() if p.is_file()})
    return sorted(p for p in paths if Path(p).name in names
                  or (Path(p).name.startswith("requirements") and p.endswith(".txt")))


def fingerprint(work, profile):
    work = Path(work)
    paths = set(inputs(work))
    for pattern in profile.get("inputs", []):
        paths.update(p.relative_to(work).as_posix() for p in work.glob(pattern) if p.is_file())
    parts = []
    for name in sorted(paths):
        file = work / name
        if file.is_file():
            if work.resolve() not in file.resolve().parents:
                raise ValueError("Dependency input escapes the worker checkout")
            parts.append((name, hashlib.sha256(file.read_bytes()).hexdigest()))
    tools = {}
    for tool in ("node", "npm"):
        if "javascript" in profile.get("tools", []) or profile.get("strategy") == "snapshot-copy":
            exe = tool + (".cmd" if platform.system() == "Windows" and tool == "npm" else "")
            result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise ValueError(f"Required tool {tool} is unavailable")
            tools[tool] = result.stdout.strip()
    value = {"profile": profile, "dependencies": parts, "python": sys.version,
             "interpreter": getattr(sys, "_base_executable", sys.executable), "tools": tools, "platform": platform.platform(),
             "machine": platform.machine(), "provisioning_runtime": "host"}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def observed(work, profile):
    work = Path(work)
    result: dict[str, Any] = {}
    for pattern in profile.get("requires", []):
        found = sorted(p for p in work.glob(pattern) if p.is_file() or p.is_dir())
        if not found:
            return None
        for path in found:
            if work.resolve() not in path.resolve().parents:
                raise ValueError("Private environment resolves outside worker")
            result[path.relative_to(work).as_posix()] = {"mtime_ns": path.stat().st_mtime_ns,
                                                      "size": path.stat().st_size}
    for pattern in ("venv/Lib/site-packages/*.dist-info/RECORD",
                    ".venv/Lib/site-packages/*.dist-info/RECORD",
                    "venv/lib/python*/site-packages/*.dist-info/RECORD",
                    ".venv/lib/python*/site-packages/*.dist-info/RECORD",
                    "node_modules/.package-lock.json"):
        for path in sorted(work.glob(pattern)):
            result[path.relative_to(work).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
