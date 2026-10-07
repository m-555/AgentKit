"""Stack-aware initialization; explicit profiles replace universal setup lists."""
from __future__ import annotations

import json
import re


def render(found):
    if not found.setup:
        return []
    javascript = [c for c in found.setup if re.match(r"(npm|pnpm|yarn)\s", c)]
    python = [c for c in found.setup if c not in javascript]
    # Unknown stack commands retain the legacy path instead of guessed profiles.
    if python and "python" not in found.stacks:
        return []
    profiles = {}
    if python:
        profiles["python"] = {"setup": python, "tools": ["python-packages"], "requires": []}
    if javascript:
        profiles["javascript"] = {"setup": javascript, "tools": ["javascript"], "requires": ["node_modules"]}
    profiles["combined"] = {"setup": [], "components": list(profiles),
                            "tools": (["python-packages"] if python else []) + (["javascript"] if javascript else []),
                            "requires": ["node_modules"] if javascript else []}
    profiles["static"] = {"setup": [], "tools": [], "requires": []}
    mapping = {}
    for level, commands in found.gates.items():
        has_js = any(re.search(r"\b(npm|npx|pnpm|yarn)\b", c) for c in commands)
        has_py = bool(python) and any("python" in c or "pytest" in c or "ruff" in c or "mypy" in c for c in commands)
        mapping[level] = "combined" if has_js and has_py else "javascript" if has_js else "python" if has_py else "static"
    return ["", "# Host preparation is selected by the checks, not by the model.",
            "environment_profiles: " + json.dumps(profiles),
            "environment_gates: " + json.dumps(mapping)]
