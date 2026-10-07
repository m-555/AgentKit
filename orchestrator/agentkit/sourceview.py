"""Read-only source access for supervised reviewers and coordinators.

Some runtimes cannot run any command inside a read-only sandbox (observed with
Codex CLI 0.159.2: `sandbox read-only` with `approval never` rejects even
`git status` and `Get-Content`). Widening the reviewer's sandbox would let it
fix what it reviews, so instead these fixed tools read *git objects of the exact
commit* rather than the filesystem:

* only files tracked at that commit are visible, so untracked and ignored files
  (local credentials, runtime state) cannot be read;
* symlinks and submodules are refused by git object mode, so nothing resolves
  outside the repository;
* credential-shaped file names are withheld, contents are redacted, and every
  read, listing, search and diff is bounded.

Authority follows the caller's supervised process: a reviewer reads only the
commit it was assigned; a coordinator reads its own job's tasks, the integration
checkout and the operator checkout's HEAD. A session with no AgentKit process
(an operator or attached external manager) may read any of them. Workers keep
their own file tools and are refused here.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import db, globs, processes, repo
from .secrets import SECRET_DIRS, SECRET_FILE_PATTERNS, redact_text

MAX_BLOB_BYTES = 2_000_000
MAX_LINES = 800
MAX_LIST = 2000
MAX_MATCHES = 200
MAX_DIFF_CHARS = 60_000
_REF = re.compile(r"[0-9a-f]{7,64}")


@dataclass(frozen=True)
class Scope:
    root: Path
    commit: str
    label: str
    base: str | None = None
    allowed: tuple[str, ...] | None = None


def _git_bytes(root: Path, args: list[str], timeout: int = 60) -> bytes:
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=str(root), capture_output=True,
                          timeout=timeout)
    if proc.returncode:
        raise ValueError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.stdout


def withheld(path: str) -> bool:
    """Credential-shaped names and AgentKit runtime state are never served."""
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if any(fnmatch.fnmatch(name, pattern.lower()) for pattern in SECRET_FILE_PATTERNS):
        return True
    lowered = "/".join(parts).lower()
    if any(f"/{d}/" in f"/{lowered}/" for d in SECRET_DIRS):
        return True
    return lowered.startswith(".ai/runtime/") or lowered.startswith(".ai/tasks.db")


def _relative(path: str) -> str:
    text = (path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ":" in text or ".." in pure.parts:
        raise ValueError(f"source path must be repository-relative: {path!r}")
    return str(pure)


# ------------------------------------------------------------------ authority


def scope(conn: sqlite3.Connection, project, *, where: str | None = None, task_id: int | None = None) -> Scope:
    """Resolve which commit the caller may read, from its process authority."""
    identifier = os.environ.get("AGENTKIT_PROCESS")
    process = None
    if not identifier and (os.environ.get("AGENTKIT_ROLE") or os.environ.get("AGENTKIT_TASK")):
        raise PermissionError("a supervised source caller requires an active control process")
    if identifier:
        process = processes.require_control(conn, purposes=("review", "coordinator"))
    if process is not None and process["purpose"] == "review":
        if (where not in (None, "task")) or (task_id is not None and task_id != process["task_id"]):
            raise PermissionError("a reviewer may read only the commit it was assigned")
        task = db.get_task(conn, int(process["task_id"]))
        if not task or not task.get("worktree") or not process.get("expected_head"):
            raise ValueError("review task has no recorded worktree or assigned commit")
        return Scope(Path(task["worktree"]), str(process["expected_head"]), f"task:{task['id']}",
                     task.get("base_sha"), tuple(task.get("expected_read", []) + task.get("expected_write", []) + task.get("owned_paths", [])))
    target = where or ("task" if task_id is not None else "integration" if process else "project")
    if target == "task":
        if task_id is None:
            raise ValueError("task_id is required to read a task checkout")
        task = db.get_task(conn, task_id)
        if not task or not task.get("worktree") or not Path(task["worktree"]).is_dir():
            raise ValueError(f"task {task_id} has no preserved worktree")
        if process is not None and task.get("job_id") != process["job_id"]:
            raise PermissionError("a coordinator may read only its own job's tasks")
        return Scope(Path(task["worktree"]), repo.head_commit(task["worktree"]), f"task:{task_id}",
                     task.get("base_sha"), tuple(task.get("expected_read", []) + task.get("expected_write", []) + task.get("owned_paths", [])))
    if target == "integration":
        from .integrator import integration_branch
        branch = integration_branch(project)
        if not repo.branch_exists(project.root, branch):
            raise ValueError("integration branch does not exist; source reads never create it")
        commit = repo._git(["rev-parse", f"refs/heads/{branch}"], project.root).strip()
        return Scope(Path(project.root), commit, "integration")
    if target == "project":
        return Scope(Path(project.root), repo.head_commit(project.root), "project")
    raise ValueError("where must be task, integration or project")


# ---------------------------------------------------------------- operations


def _entries(view: Scope) -> dict[str, tuple[str, str, int]]:
    """path -> (mode, object id, size) for regular tracked files at the commit."""
    if not _REF.fullmatch(view.commit):
        raise ValueError("source commit must be a recorded object id")
    raw = _git_bytes(view.root, ["ls-tree", "-r", "-l", "-z", "--full-tree", view.commit])
    found: dict[str, tuple[str, str, int]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, sha, size = meta.decode().split()
        path = name.decode("utf-8", "replace")
        # 120000 is a symlink and 160000 a submodule: neither is served.
        if (kind == "blob" and mode in ("100644", "100755") and not withheld(path)
                and (view.allowed is None or globs.matches_any(view.allowed, path))):
            found[path] = (mode, sha, int(size) if size.isdigit() else 0)
    return found


def listing(view: Scope, *, prefix: str = "", pattern: str = "", offset: int = 0, limit: int = 500) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= MAX_LIST:
        raise ValueError(f"limit must be 1..{MAX_LIST} and offset non-negative")
    base = _relative(prefix).rstrip("/") + "/" if prefix.strip() not in ("", ".") else ""
    entries = _entries(view)
    paths = sorted(p for p in entries if p.startswith(base) and (not pattern or fnmatch.fnmatch(p, pattern)))
    page = paths[offset:offset + limit]
    return {"scope": view.label, "commit": view.commit, "total": len(paths),
            "files": [{"path": p, "size": entries[p][2]} for p in page],
            "next_offset": offset + limit if offset + limit < len(paths) else None}


def read(view: Scope, path: str, *, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
    rel = _relative(path)
    if withheld(rel):
        raise PermissionError(f"{rel} is withheld: credential-shaped files and runtime state are never served")
    if start_line < 1 or not 1 <= max_lines <= MAX_LINES:
        raise ValueError(f"start_line must be >= 1 and max_lines 1..{MAX_LINES}")
    entry = _entries(view).get(rel)
    if entry is None:
        raise ValueError(f"{rel} is not a regular tracked file at {view.commit[:12]} ({view.label})")
    _, sha, size = entry
    if size > MAX_BLOB_BYTES:
        raise ValueError(f"{rel} is {size} bytes; files over {MAX_BLOB_BYTES} bytes are not served")
    data = _git_bytes(view.root, ["cat-file", "blob", sha])
    if b"\0" in data[:8000]:
        return {"scope": view.label, "commit": view.commit, "path": rel, "binary": True, "size": size}
    lines = _safe_lines(data.decode("utf-8", "replace"))
    chosen = lines[start_line - 1:start_line - 1 + max_lines]
    text = "\n".join(f"{start_line + i}: {line}" for i, line in enumerate(chosen))
    following = start_line + len(chosen)
    return {"scope": view.label, "commit": view.commit, "path": rel, "total_lines": len(lines),
            "content": redact_text(text), "next_line": following if following <= len(lines) else None}


def _safe_lines(text: str) -> list[str]:
    # Whole-document context catches keys whose delimiter is outside the page.
    text = re.sub(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----",
                  lambda m: "\n".join("[redacted]" for _ in m.group().splitlines()), text)
    return redact_text(text).splitlines()


def search(view: Scope, text: str, *, path_glob: str = "", limit: int = 100) -> dict[str, Any]:
    """Fixed-string search at the commit; regular expressions are not accepted."""
    if not text or len(text) > 200:
        raise ValueError("search text must be 1..200 characters")
    if not 1 <= limit <= MAX_MATCHES:
        raise ValueError(f"limit must be 1..{MAX_MATCHES}")
    entries = _entries(view)
    matches = []
    for path, (_, sha, size) in sorted(entries.items()):
        if size > MAX_BLOB_BYTES or (path_glob and not fnmatch.fnmatch(path, path_glob)):
            continue
        data = _git_bytes(view.root, ["cat-file", "blob", sha])
        if b"\0" in data[:8000]:
            continue
        for number, content in enumerate(_safe_lines(data.decode("utf-8", "replace")), 1):
            if text in content:
                matches.append({"path": path, "line": number, "text": content[:400]})
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    return {"scope": view.label, "commit": view.commit, "matches": matches, "truncated": len(matches) >= limit}


def diff(view: Scope, *, path: str = "", offset: int = 0, limit: int = MAX_DIFF_CHARS) -> dict[str, Any]:
    """The task's committed change against its recorded base, secret paths excluded."""
    if not view.base or not _REF.fullmatch(view.base):
        raise ValueError("diff needs a task scope with a recorded base commit")
    if offset < 0 or not 1 <= limit <= MAX_DIFF_CHARS:
        raise ValueError(f"limit must be 1..{MAX_DIFF_CHARS}")
    names = _git_bytes(view.root, ["diff", "--name-only", "--no-renames", "-z", view.base, view.commit])
    changed = [n.decode("utf-8", "replace") for n in names.split(b"\0") if n]
    if path:
        rel = _relative(path)
        changed = [n for n in changed if n == rel]
    entries = _entries(view)
    before = _entries(Scope(view.root, view.base, view.label, allowed=view.allowed))
    served = [n for n in changed if n in entries or n in before]
    def safe(entry):
        if not entry:
            return []
        if entry[2] > MAX_BLOB_BYTES:
            return ["[file exceeds source size bound]\n"]
        data = _git_bytes(view.root, ["cat-file", "blob", entry[1]])
        if b"\0" in data[:8000]:
            return ["[binary file]\n"]
        return [line + "\n" for line in _safe_lines(data.decode("utf-8", "replace"))]
    patches: list[str] = []
    for name in served:
        patches.extend(difflib.unified_diff(safe(before.get(name)), safe(entries.get(name)),
                                            fromfile="a/" + name, tofile="b/" + name))
    text = "".join(patches)
    commits = redact_text(repo._git(["log", "--oneline", f"{view.base}..{view.commit}"], view.root)).splitlines()
    return {"scope": view.label, "base": view.base, "commit": view.commit, "commits": commits[:200],
            "changed": served, "withheld": sorted(set(changed) - set(served)),
            "diff": text[offset:offset + limit], "total_characters": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None}
