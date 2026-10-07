"""Opt-in npm snapshots: private copies, never writable links to another checkout."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from .environment_capacity import cache_root
from .locking import atomic_write


def _linked(path):
    attributes = path.lstat()
    return path.is_symlink() or bool(getattr(attributes, "st_file_attributes", 0)
                                   & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _packages(work):
    from .environment_profiles import inputs
    result = {}
    for name in inputs(work):
        if Path(name).name != "package.json" or name == "package.json":
            continue
        value = json.loads((work / name).read_text())
        if value.get("name"):
            result[value["name"]] = Path(name).parent.as_posix()
    return result


def _snapshot(project, key):
    return cache_root(project) / "npm-snapshots" / key


def eligible(work, profile):
    if profile["strategy"] != "snapshot-copy":
        return False
    if len(profile["setup"]) != 1 or not profile["setup"][0].startswith("npm ci"):
        raise ValueError("snapshot-copy requires exactly one npm ci setup command")
    if any(char in profile["setup"][0] for char in ("&", ";", "|", "\n")):
        raise ValueError("snapshot-copy cannot skip compound setup commands")
    if not (work / "package-lock.json").is_file():
        raise ValueError("snapshot-copy requires a committed npm lockfile")
    from .environment_profiles import inputs
    for name in inputs(work):
        if Path(name).name != "package.json":
            continue
        value = json.loads((work / name).read_text())
        if any(k in value.get("scripts", {}) for k in ("install", "preinstall", "postinstall", "prepare")):
            raise ValueError("Workspace lifecycle scripts require private-cache setup")
    return True


def _link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.symlink_to(target, target_is_directory=True)
        return
    # Literal paths, no shell string interpolation or traversal through a link.
    script = "param($link,$target) New-Item -ItemType Junction -Path $link -Target $target | Out-Null"
    tool = path.parent / (".agentkit-junction-" + uuid.uuid4().hex + ".ps1")
    try:
        tool.write_text(script)
        subprocess.run(["powershell", "-NoProfile", "-File", str(tool), str(path), str(target)],
                       capture_output=True, check=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        tool.unlink(missing_ok=True)


def capture(project, work, key):
    source = work / "node_modules"
    if not source.is_dir() or _linked(source):
        raise ValueError("Snapshot source must be a private node_modules directory")
    target = _snapshot(project, key)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (key + ".partial-" + uuid.uuid4().hex)
    links = {}
    packages = _packages(work)

    def ignore(base, names):
        skipped = []
        for name in names:
            entry = Path(base) / name
            if _linked(entry):
                relative = entry.relative_to(source).as_posix()
                resolved = entry.resolve()
                matches = [p for p in packages.values() if (work / p).resolve() == resolved]
                if len(matches) != 1:
                    raise ValueError("Snapshot contains an unrecognized dependency link")
                links[relative] = matches[0]
                skipped.append(name)
        return skipped
    try:
        shutil.copytree(source, temporary / "node_modules", ignore=ignore)
        atomic_write(temporary / "receipt.json", json.dumps({"key": key, "links": links}) + "\n")
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def restore(project, work, key):
    source = _snapshot(project, key)
    receipt = source / "receipt.json"
    if not receipt.is_file():
        return False
    value = json.loads(receipt.read_text())
    if value["key"] != key or not (source / "node_modules").is_dir():
        raise ValueError("Invalid npm snapshot receipt")
    target = work / "node_modules"
    if target.exists():
        return False  # Never overwrite a partial installation or worker changes.
    packages = set(_packages(work).values())
    for relative, checkout in value["links"].items():
        if checkout not in packages or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Snapshot workspace link does not match this checkout")
    if _linked(source / "node_modules"):
        raise ValueError("Cached snapshot root must be private")
    def reject_links(base, names):
        if any(_linked(Path(base) / name) for name in names):
            raise ValueError("Cached snapshot contains an unexpected link")
        return []
    shutil.copytree(source / "node_modules", target, ignore=reject_links)
    for relative, checkout in value["links"].items():
        _link(target / relative, work / checkout)
    return True
