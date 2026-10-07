"""Creating worker worktrees, and deciding what may be shared between them.

v2 symlinked `node_modules` and `.venv` across worktrees to save disk. That is a
correctness bug, not an optimisation: one worker running `npm install` mutates
every other worker's environment, and the resulting failures surface in
*unrelated* tasks, which is close to the worst possible debugging experience.

v3 shares only what is genuinely immutable:

* package **download caches** â€” content-addressed, concurrent-safe, always shared
* dependency **environments** â€” **never** shared between active worktrees
* everything else â€” private

The environment rule is stricter than fingerprint-gating, and deliberately so.
Matching lockfiles prove dependency *equivalence*, not *immutability*: editable
installs, native module rebuilds, postinstall scripts, `pip install -e`, test
fixtures writing into site-packages and tooling caches all mutate a `.venv` or
`node_modules` whose lockfile never changed. A fingerprint cannot detect any of
that, so it is not a safe basis for sharing a writable directory.

The fingerprint is still computed and still useful â€” as a cache key, for
provisioning reuse, and for detecting an environment that has gone stale â€” it is
simply no longer treated as permission to share one.
"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repo, workspace_registry
from .config import ProjectConfig

LOCKFILES = (
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt", "requirements-dev.txt",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock",
)

#: Installed environments and build output. Always private to one worktree.
ENV_DIRS = (".venv", "venv", "node_modules", "vendor", "target", "build", "dist")


@dataclass
class Provision:
    worktree: Path
    created: bool
    fingerprint: str
    shared_env: bool
    reason: str


def slug(task: dict[str, Any]) -> str:
    spec_id = task.get("spec_id") or f"task-{task.get('id')}"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(spec_id)).strip("-")[:48] or f"task-{task.get('id')}"


def branch_name(task: dict[str, Any]) -> str:
    return str(task.get("branch") or f"agent/{slug(task)}-{workspace_registry.suffix(task)}")


def path_for(root: str | Path, task: dict[str, Any], project=None) -> Path:
    """Recorded path or grouped external storage, never inside the source checkout."""
    root_path = Path(root).resolve()
    saved = workspace_registry.stored(root_path, task)
    recorded = task.get("worktree") or (saved or {}).get("path")
    if recorded:
        return Path(recorded)
    from .worktree_storage import destination
    return destination(root_path, task, project)


def environment_fingerprint(root: str | Path) -> str:
    """hash(lockfiles + runtime versions + platform) â€” Â§10.2."""
    digest = hashlib.sha256()
    root_path = Path(root)
    for name in sorted(LOCKFILES):
        candidate = root_path / name
        if candidate.is_file():
            try:
                digest.update(name.encode("utf-8"))
                digest.update(candidate.read_bytes())
            except OSError:
                continue
    pyproject = root_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if any(k in line for k in ("dependencies", "requires-python", "==", ">=")):
                    digest.update(line.strip().encode("utf-8"))
        except OSError:
            pass
    digest.update(sys.version.split()[0].encode("utf-8"))
    digest.update(_node_version().encode("utf-8"))
    digest.update(f"{platform.system()}-{platform.machine()}".encode())
    return digest.hexdigest()[:16]


def _node_version() -> str:
    try:
        proc = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "no-node"
    return (proc.stdout or "").strip() or "no-node"


def may_share_environment(
    conn: Any, project: ProjectConfig, task: dict[str, Any]
) -> tuple[bool, str]:
    """Always False. Installed environments are private to a worktree.

    Kept as a function rather than deleted because callers and tests read it as
    the policy statement, and because the reason should travel with the answer.
    """
    return False, (
        "installed environments are never shared between active worktrees: matching "
        "lockfiles prove dependency equivalence, not that nothing will mutate the "
        "installed tree (editable installs, native rebuilds, postinstall scripts)"
    )


def ensure(
    root: str | Path, task: dict[str, Any], project: ProjectConfig | None = None
) -> tuple[Path, bool]:
    """Create or adopt this task's worktree. Idempotent (Â§14)."""
    root_path = Path(root).resolve()
    target = path_for(root_path, task, project)
    saved = workspace_registry.stored(root_path, task)
    branch = str((saved or {}).get("branch") or branch_name(task))
    workspace_registry.record(root_path, task, path=str(target), branch=branch, phase="reserved")

    if target.is_dir() and (target / ".git").exists():
        registered = {Path(e["worktree"]).resolve() for e in repo.worktree_list(root_path)}
        if target.resolve() not in registered:
            raise ValueError("Existing checkout belongs to another Git repository")
        if repo.current_branch(target) != branch:
            raise ValueError("Existing worktree branch differs from its durable reservation")
        workspace_registry.record(root_path, task, phase="preserved", head=repo.head_commit(target))
        return target, False

    existing = {Path(e.get("worktree", "")).resolve(): e for e in repo.worktree_list(root_path)
                if e.get("worktree")}
    if target.resolve() in existing:
        return target, False

    args = ["worktree", "add"]
    if repo.branch_exists(root_path, branch):
        args += [str(target), branch]
    else:
        from .integrator import ensure_integration_branch
        base = ensure_integration_branch(project or ProjectConfig(root=root_path))
        args += ["-b", branch, str(target), base]

    try:
        subprocess.run(["git", *args], cwd=str(root_path), capture_output=True,
                       text=True, timeout=300, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git worktree add failed: {(exc.stderr or '').strip() or exc}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git worktree add failed: {exc}") from exc

    workspace_registry.record(root_path, task, phase="created", head=repo.head_commit(target))
    return target, True


def provision(
    conn: Any, root: str | Path, task: dict[str, Any], project: ProjectConfig
) -> Provision:
    """Create the worktree and link only what is safe to link."""
    root_path = Path(root).resolve()
    worktree, created = ensure(root_path, task, project)
    fingerprint = environment_fingerprint(root_path)
    shared, reason = may_share_environment(conn, project, task)

    # No environment linking, by policy. Shared *caches* are supplied through the
    # worker's environment variables instead of symlinks, so a worktree teardown
    # can never follow a link into a shared directory and delete it.
    return Provision(
        worktree=worktree, created=created, fingerprint=fingerprint,
        shared_env=shared, reason=reason,
    )


def shared_cache_env(root: str | Path) -> dict[str, str]:
    """Environment variables pointing every worker at one download cache.

    Content-addressed caches are safe to share concurrently â€” that is what they
    are designed for â€” and sharing them recovers most of the disk and time cost
    of private environments. Using env vars rather than symlinks means a worktree
    can be removed without any chance of deleting the shared cache with it.
    """
    from .config import load_project
    from .environment_capacity import cache_root
    base = cache_root(load_project(root))
    uv_cache = base / "uv"
    npm_cache = base / "npm"
    pip_cache = base / "pip"
    for path in (uv_cache, npm_cache, pip_cache):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "UV_CACHE_DIR": str(uv_cache),
        "npm_config_cache": str(npm_cache),
        "PIP_CACHE_DIR": str(pip_cache),
    }


def private_env_paths(worktree: str | Path) -> list[Path]:
    """Directories that must exist per worktree and never be shared."""
    return [Path(worktree) / name for name in ENV_DIRS]


def remove(root: str | Path, task: dict[str, Any], *, force: bool = False) -> bool:
    target = path_for(root, task)
    if not target.exists():
        return False
    args = ["worktree", "remove", str(target)]
    if force:
        args.append("--force")
    try:
        subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                       text=True, timeout=300, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
