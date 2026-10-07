"""Path roles and the one canonicalisation everything authorises against.

Two distinct jobs live here, and conflating them has caused real bugs:

**Path roles (§7).** Four different directories get confused constantly:

    repo_common_root   the main checkout — owns .ai/ and tasks.db
    worktree_root      the linked worktree a worker actually edits in
    state_root         where runtime state lives (== repo_common_root)
    project_root       where committed config lives (== repo_common_root)

`tasks.db` is gitignored, so it exists only in the main checkout. A worker that
resolved state to its own worktree silently created a second, empty database and
saw no leases. These are now named functions rather than ad-hoc rediscovery.

**Canonicalisation (§9).** Exactly one function decides what a path *is* before
anyone authorises it: `canonical_relpath`. Authorisation reasons about resolved
filesystem targets, not string appearance, so `a/../../escape`, `C:\\x` on POSIX,
`//server/share`, a symlink pointing out of the tree, and `SERVICES/Media.PY` on
a case-insensitive filesystem all reduce to one answer — or to None, which every
caller must treat as refusal, never as "unknown, allow".
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path

AI_DIR = ".ai"

_DRIVE = re.compile(r"^[A-Za-z]:")
_UNC = re.compile(r"^[/\\]{2}[^/\\]")

#: Windows silently strips these; "secret.env." and "secret.env" are the same file.
_WINDOWS_TRAILING = " ."


def normalize(path: str | Path) -> str:
    """Forward slashes, no leading './', no trailing '/'. Display and matching only.

    This is *not* an authorisation primitive — it never touches the filesystem.
    Use `canonical_relpath` for anything that gates a write.
    """
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/{2,}", "/", text)
    return text.rstrip("/") or "."


# --------------------------------------------------------------- path roles


def git_root(start: str | Path | None = None) -> Path | None:
    """The top of *this* working tree — a linked worktree returns itself."""
    return _git_path(start, ["rev-parse", "--show-toplevel"])


def worktree_root(start: str | Path | None = None) -> Path | None:
    """The checkout a worker edits in. May be a linked worktree."""
    return git_root(start)


def repo_common_root(start: str | Path | None = None) -> Path | None:
    """The main checkout, even when called from inside a linked worktree.

    `--git-common-dir` points every worktree at the one real `.git`, which is how
    all of them share a single authority for runtime state.
    """
    common = _git_path(start, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return common.parent if common else None


#: Kept for callers that predate the rename.
main_worktree_root = repo_common_root


def _git_path(start: str | Path | None, args: list[str]) -> Path | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(start or os.getcwd()),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    if not text:
        return None
    try:
        return Path(text).resolve()
    except (OSError, ValueError):
        return None


def find_project_root(start: str | Path | None = None) -> Path | None:
    """Where committed config and runtime state live — always the main checkout."""
    here = Path(start or os.getcwd()).resolve()

    common = repo_common_root(here)
    if common and (common / AI_DIR / "project.yaml").is_file():
        return common

    for candidate in (here, *here.parents):
        if (candidate / AI_DIR / "project.yaml").is_file():
            return candidate

    if common:
        return common
    for candidate in (here, *here.parents):
        if (candidate / AI_DIR).is_dir():
            return candidate
    return git_root(here)


def state_root(start: str | Path | None = None) -> Path | None:
    """Where `tasks.db`, leases and the event log live. Never a linked worktree."""
    return find_project_root(start)


def ai_dir(root: str | Path) -> Path:
    return Path(root) / AI_DIR


def is_managed(root: str | Path | None) -> bool:
    """True when this repository has been onboarded (§3's managed/unmanaged split)."""
    return bool(root and (Path(root) / AI_DIR / "project.yaml").is_file())


# ------------------------------------------------------- canonicalisation


def is_absolute_like(raw: str) -> bool:
    """Absolute on *either* platform.

    `Path("/etc/passwd").is_absolute()` is False on Windows because there is no
    drive letter, so a POSIX-absolute path would otherwise be treated as
    relative and silently pass an ownership check it should fail.
    """
    if not raw:
        return False
    if _UNC.match(raw):
        return True
    if raw.startswith(("/", "\\")):
        return True
    if _DRIVE.match(raw):
        return True
    try:
        return Path(raw).is_absolute()
    except (OSError, ValueError):
        return False


def _fold(text: str) -> str:
    """Reduce Unicode and Windows filename quirks to one comparable form.

    NFC because macOS hands back NFD; casefold because Windows and macOS are
    case-insensitive and `.ENV` must not evade a rule written for `.env`. The
    folded form is used for *comparison*, never as the stored path.
    """
    return unicodedata.normalize("NFC", text).casefold()


def _strip_windows_junk(segment: str) -> str:
    """`secret.env.` and `secret.env ` open the same file on Windows."""
    if os.name != "nt":
        return segment
    stripped = segment.rstrip(_WINDOWS_TRAILING)
    return stripped or segment


def canonical_relpath(
    path: str | Path, root: str | Path, *, allow_missing: bool = True
) -> str | None:
    """The one function authorisation reasons about. None means **refuse**.

    Returns a repo-relative, forward-slash path, or None when the target is
    outside `root` — including via `..`, an absolute path, a UNC share, or a
    symlink/junction whose destination escapes the tree.

    `allow_missing` lets a not-yet-created file be authorised (an agent writing a
    new file); its *parent* chain is still resolved, so a symlinked directory
    cannot be used to smuggle a write out of the tree.
    """
    raw = str(path).strip()
    if not raw:
        return None

    root_path = Path(root)
    try:
        root_real = root_path.resolve(strict=False)
    except (OSError, ValueError):
        return None

    candidate = Path(raw)
    if not is_absolute_like(raw):
        candidate = root_real / raw

    resolved = _resolve_strict_enough(candidate, allow_missing=allow_missing)
    if resolved is None:
        return None

    try:
        relative = resolved.relative_to(root_real)
    except ValueError:
        if not _same_tree_casefolded(resolved, root_real):
            return None
        try:
            relative = Path(str(resolved)[len(str(root_real)):].lstrip("/\\"))
        except (OSError, ValueError):
            return None

    rel = normalize(relative)
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None
    # Final fail-closed guard. `pathlib` join semantics mean a segment carrying a
    # drive or root ("C:", "//server") *replaces* what it is joined to, so a
    # crafted path can resolve outside the tree and still come back looking
    # relative. Anything still absolute-shaped here is refused rather than
    # reasoned about — found by the property test in test_acceptance_paths.py.
    if is_absolute_like(rel):
        return None
    return rel


def _resolve_strict_enough(candidate: Path, *, allow_missing: bool) -> Path | None:
    """Resolve symlinks as far as the path actually exists.

    A fully strict resolve refuses paths that do not exist yet, which would block
    an agent creating a new file. Resolving the deepest existing ancestor and
    re-appending the remainder keeps symlink safety without that cost.
    """
    try:
        return candidate.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        if not allow_missing:
            return None

    parts: list[str] = []
    probe = candidate
    for _ in range(64):
        parent = probe.parent
        if parent == probe:
            return None
        parts.append(_strip_windows_junk(probe.name))
        probe = parent
        try:
            if probe.exists():
                base = probe.resolve(strict=True)
                break
        except (OSError, ValueError, RuntimeError):
            return None
    else:
        return None

    result = base
    for name in reversed(parts):
        if name in ("", "."):
            continue
        if name == "..":
            result = result.parent
            continue
        # A segment that carries a drive or root would *replace* `result` rather
        # than extend it (`Path("D:/repo") / "C:"` is `C:`). Refuse instead.
        if is_absolute_like(name) or "/" in name or "\\" in name:
            return None
        result = result / name
    return result


def _same_tree_casefolded(resolved: Path, root: Path) -> bool:
    """Last resort for case-insensitive filesystems where relative_to fails."""
    if os.name != "nt":
        return False
    return _fold(str(resolved)).startswith(_fold(str(root)) + os.sep.casefold())


def escapes_root(path: str | Path, root: str | Path) -> bool:
    return canonical_relpath(path, root) is None


def is_symlink_escape(path: str | Path, root: str | Path) -> bool:
    """True when any component is a link whose destination leaves the tree.

    Checked explicitly so the reason can be reported, rather than folded into a
    generic "outside the repository".
    """
    root_real = Path(root).resolve(strict=False)
    probe = Path(path)
    if not is_absolute_like(str(path)):
        probe = root_real / str(path)
    for _ in range(64):
        if probe.is_symlink():
            try:
                target = probe.resolve(strict=False)
            except (OSError, ValueError, RuntimeError):
                return True
            try:
                target.relative_to(root_real)
            except ValueError:
                return True
        parent = probe.parent
        if parent == probe:
            return False
        probe = parent
    return False


def relative_to_root(path: str | Path, root: str | Path) -> str | None:
    return canonical_relpath(path, root)


def resolve_within(path: str | Path, root: str | Path) -> str | None:
    """Alias retained for existing call sites. Always canonicalises."""
    return canonical_relpath(path, root)


@lru_cache(maxsize=2048)
def fold_for_match(path: str) -> str:
    """Folded form used when matching patterns on case-insensitive systems."""
    return _fold(normalize(path))
