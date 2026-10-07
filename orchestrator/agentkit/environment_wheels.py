"""Opt-in frozen wheel sets: prepare once, build private venvs at their final paths."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .environment_capacity import cache_root
from .locking import atomic_write


def _python(work, profile):
    relative = profile.get("python", "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    path = work / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Profile python must be inside its private worktree environment")
    return path


def _directory(project, key):
    return cache_root(project) / "python-wheels" / key


def eligible(profile):
    return profile["strategy"] == "python-wheels"


def _run(argv, *, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr)[-4000:])
    return result.stdout


def capture(project, work, profile, key):
    python = _python(work, profile)
    target = _directory(project, key)
    if target.exists():
        return
    frozen = _run([str(python), "-m", "pip", "freeze", "--all"], cwd=work)
    pins = [line.strip() for line in frozen.splitlines() if line.strip()]
    if not pins or any(not re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line) for line in pins):
        raise ValueError("Wheel reuse requires version-pinned packages without editable or local installs")
    temporary = target.parent / (key + ".partial-" + uuid.uuid4().hex)
    temporary.mkdir(parents=True)
    try:
        atomic_write(temporary / "requirements.lock", "\n".join(pins) + "\n")
        _run([str(python), "-m", "pip", "wheel", "--wheel-dir", str(temporary / "wheels"),
              "--requirement", str(temporary / "requirements.lock")], cwd=work)
        atomic_write(temporary / "receipt.json", json.dumps({"key": key, "pins": pins}) + "\n")
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def restore(project, work, profile, key):
    cache = _directory(project, key)
    receipt = cache / "receipt.json"
    if not receipt.is_file():
        return False
    value = json.loads(receipt.read_text())
    if value.get("key") != key or not (cache / "wheels").is_dir():
        raise ValueError("Invalid frozen wheel receipt")
    python = _python(work, profile)
    if python.exists():
        return False  # Preserve partial/changed environments until explicit repair.
    environment = python.parent.parent
    _run([sys.executable, "-m", "venv", str(environment)], cwd=work)
    _run([str(python), "-m", "pip", "install", "--no-index", "--find-links",
          str(cache / "wheels"), "--requirement", str(cache / "requirements.lock")], cwd=work)
    return True
