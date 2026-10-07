"""CONTRACT_CHANGE — freezing a shared shape before dependents fan out.

A contract change is not a normal task: its blast radius is every task that codes
against it. Treated as ordinary parallel work, three agents implement three
readings of an interface that is still moving, which is the classic failure of
parallel frontend/backend development.

Invariant 13: contracts freeze before dependents fan out. `contracts.lock` records
the hash of every frozen path, dependents hold a `shared-read` lease on those
paths, and the merge gate re-hashes them. A dependent physically cannot change a
frozen contract: the write is denied at L3/L4 and the hash mismatch is caught at L7.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, globs
from . import statemachine as sm
from .config import ProjectConfig
from .paths import ai_dir, normalize


@dataclass
class ContractLock:
    version: int
    frozen_at: str
    owner_task: str
    paths: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "frozen_at": self.frozen_at,
            "owner_task": self.owner_task,
            "paths": dict(self.paths),
        }


def lock_path(root: str | Path) -> Path:
    return ai_dir(Path(root)) / "contracts.lock"


def load_lock(root: str | Path) -> ContractLock | None:
    path = lock_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ContractLock(
        version=int(data.get("version") or 0),
        frozen_at=str(data.get("frozen_at") or ""),
        owner_task=str(data.get("owner_task") or ""),
        paths={str(k): str(v) for k, v in (data.get("paths") or {}).items()},
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        digest.update(path.read_bytes())
    except OSError:
        return ""
    return "sha256:" + digest.hexdigest()


def contract_files(root: str | Path, project: ProjectConfig) -> list[str]:
    """Every tracked file matching the project's `contracts:` patterns."""
    from . import repo

    root_path = Path(root)
    tracked = repo.tracked_files(root_path)
    return sorted(p for p in tracked if globs.matches_any(project.contracts, p))


def freeze(
    conn: sqlite3.Connection,
    root: str | Path,
    project: ProjectConfig,
    task_id: int,
    *,
    version: int | None = None,
    record: bool = True,
) -> ContractLock:
    """Record the hash of every contract path and fan the version out.

    Called after a human or the architect approves the proposed contract — this
    is one of §16's explicit human control points, because freezing commits every
    dependent task to the shape.
    """
    root_path = Path(root)
    task = db.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} does not exist")
    if str(task.get("kind")) != "CONTRACT_CHANGE":
        raise ValueError(
            f"task {task_id} is {task.get('kind')}, not CONTRACT_CHANGE. Only a "
            "contract task may freeze a contract."
        )

    previous = load_lock(root_path)
    next_version = version if version is not None else ((previous.version if previous else 0) + 1)

    paths = {}
    for rel in contract_files(root_path, project):
        digest = hash_file(root_path / rel)
        if digest:
            paths[normalize(rel)] = digest

    lock = ContractLock(
        version=next_version,
        frozen_at=datetime.now(UTC).isoformat(timespec="seconds"),
        owner_task=str(task.get("spec_id") or task_id),
        paths=paths,
    )
    lock_path(root_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path(root_path).write_text(
        json.dumps(lock.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    if record:
        record_lock(conn, task_id, lock)
    return lock


def record_lock(conn: sqlite3.Connection, task_id: int, lock: ContractLock) -> None:
    """Publish a frozen version after the integration commit passes verification."""
    task = db.get_task(conn, task_id)
    if task is None:
        raise ValueError("contract task no longer exists")
    db.update_task(conn, task_id, contract_version=lock.version)
    db.log_event(
        conn, task_id, "contract_frozen",
        cause=f"version {lock.version}",
        effect=f"{len(lock.paths)} contract path(s) hashed; dependents may now fan out",
        detail={"version": lock.version, "paths": sorted(lock.paths)},
    )
    _fan_out(conn, task, lock.version)


def _fan_out(conn: sqlite3.Connection, owner: dict[str, Any], version: int) -> None:
    """Stamp the version onto dependents and give them read-only contract leases."""
    identifiers = {str(owner.get("id")), str(owner.get("spec_id") or "")}
    for task in db.list_tasks(conn):
        deps = {str(d) for d in (task.get("depends_on") or [])}
        if not (deps & identifiers):
            continue
        db.update_task(conn, int(task["id"]), contract_version=version)
        db.log_event(
            conn, int(task["id"]), "contract_version_assigned",
            cause=f"depends on contract task {owner.get('spec_id') or owner.get('id')}",
            effect=f"pinned to contract version {version}",
        )


def verify_unchanged(
    root: str | Path, project: ProjectConfig, *, exclude_owner: str | None = None
) -> list[str]:
    """Which frozen contract paths no longer match their recorded hash.

    Run at the merge gate. A mismatch on any task other than the owning
    CONTRACT_CHANGE is an automatic reject (§12.2 step 5).
    """
    lock = load_lock(root)
    if lock is None:
        return []
    if exclude_owner and lock.owner_task == exclude_owner:
        return []
    root_path = Path(root)
    changed: list[str] = []
    for rel, expected in lock.paths.items():
        actual = hash_file(root_path / rel)
        if actual != expected:
            changed.append(rel)
    return sorted(changed)


def stale_dependents(conn: sqlite3.Connection, current_version: int) -> list[dict[str, Any]]:
    """Tasks pinned to a superseded contract version."""
    stale = []
    for task in db.list_tasks(conn):
        pinned = task.get("contract_version")
        if (
            pinned is not None and int(pinned) < current_version
            and str(task["status"]) not in sm.TERMINAL
        ):
            stale.append(task)
    return stale


def supersede(
    conn: sqlite3.Connection, root: str | Path, project: ProjectConfig, task_id: int
) -> list[int]:
    """Freeze a new version and mark every dependent on the old one NEEDS_REPLAN.

    Deliberately expensive: making contract churn visible is the only thing that
    discourages it (§12.3).
    """
    # Capture who is pinned to the current version *before* freezing: `freeze`
    # fans the new version out to dependents, which would otherwise make every
    # one of them look up to date.
    previous = load_lock(root)
    previous_version = previous.version if previous else 0
    pinned = [
        task for task in db.list_tasks(conn)
        if task.get("contract_version") is not None
        and int(task["contract_version"]) <= previous_version
        and str(task["status"]) not in sm.TERMINAL
        and int(task["id"]) != task_id
    ]

    lock = freeze(conn, root, project, task_id)
    replanned: list[int] = []
    for task in pinned:
        if sm.can(str(task["status"]), sm.NEEDS_REPLAN, "scheduler"):
            db.set_status(
                conn, int(task["id"]), sm.NEEDS_REPLAN, actor="scheduler",
                cause=f"contract superseded by version {lock.version}",
            )
            db.release_leases(conn, int(task["id"]), reason="contract superseded")
            replanned.append(int(task["id"]))
    return replanned


def contract_lease_mode(task: dict[str, Any], project: ProjectConfig, path: str) -> str | None:
    """`shared-read` for dependents, `exclusive-write` for the contract owner."""
    if not globs.matches_any(project.contracts, path):
        return None
    return "exclusive-write" if str(task.get("kind")) == "CONTRACT_CHANGE" else "shared-read"
