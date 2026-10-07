"""SQLite runtime state.

Authority (PLAN_V3 §4.1): this database owns *execution* state only — status,
leases, attempts, spend, heartbeats, worker runs, events. It owns nothing a
human wrote and nothing git knows. That is what makes §4.3's "delete the database
and rebuild" a supported operation rather than a disaster.

Only this module touches SQLite. Agents reach it through MCP, hooks through
`agentkit-hook`, humans through the CLI. A model that can edit `tasks.db`
directly can edit its way out of its own lease.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import statemachine as sm
from .paths import ai_dir
from .secrets import redact

SCHEMA_VERSION = 7

TASK_STATES = sm.STATES
ACTIVE_STATES = sm.ACTIVE

TASK_KINDS = (
    "SAFE_PARALLEL", "DEPENDENT", "HOTSPOT", "DECOUPLE",
    "CONTRACT_CHANGE", "TEST_ONLY", "RESEARCH",
    # A human holding leases through the same machinery as any worker (§3).
    "OPERATOR",
)

LEASE_MODES = ("exclusive-write", "shared-read", "advisory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id          TEXT    UNIQUE,
    spec_hash        TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL,
    description      TEXT    NOT NULL DEFAULT '',
    kind             TEXT    NOT NULL DEFAULT 'SAFE_PARALLEL',
    status           TEXT    NOT NULL DEFAULT 'PLANNED',
    role             TEXT    NOT NULL DEFAULT 'implementer',
    generation       INTEGER NOT NULL DEFAULT 0,
    adapter          TEXT,
    model            TEXT,
    branch           TEXT,
    worktree         TEXT,
    session_token    TEXT,
    owned_paths      TEXT    NOT NULL DEFAULT '[]',
    expected_write   TEXT    NOT NULL DEFAULT '[]',
    expected_read    TEXT    NOT NULL DEFAULT '[]',
    depends_on       TEXT    NOT NULL DEFAULT '[]',
    gate_level       TEXT    NOT NULL DEFAULT 'fast',
    priority         INTEGER NOT NULL DEFAULT 100,
    attempts         INTEGER NOT NULL DEFAULT 0,
    spend_usd        REAL    NOT NULL DEFAULT 0,
    budget_usd       REAL,
    base_sha         TEXT,
    last_commit      TEXT,
    next_action      TEXT,
    blocker          TEXT,
    blocked_meta     TEXT,
    contract_version INTEGER,
    heartbeat_at     TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    generation  INTEGER NOT NULL DEFAULT 0,
    path_glob   TEXT    NOT NULL,
    mode        TEXT    NOT NULL DEFAULT 'exclusive-write',
    acquired_at TEXT    NOT NULL,
    expires_at  TEXT,
    heartbeat_at TEXT,
    ttl_seconds INTEGER NOT NULL DEFAULT 600,
    released_at TEXT,
    released_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_active ON leases(released_at, task_id);

-- One row per worker launch. UNIQUE(task_id, generation) is the idempotency key
-- that makes a double `agentkit run` a no-op instead of two workers (§14).
CREATE TABLE IF NOT EXISTS worker_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    generation  INTEGER NOT NULL,
    adapter     TEXT    NOT NULL DEFAULT '',
    pid         INTEGER,
    worktree    TEXT,
    started_at  TEXT    NOT NULL,
    ended_at    TEXT,
    exit_code   INTEGER,
    UNIQUE(task_id, generation)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL DEFAULT 'mechanical',
    reason     TEXT    NOT NULL DEFAULT 'manual',
    head_sha   TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, kind, id DESC);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    kind    TEXT NOT NULL,
    cause   TEXT NOT NULL DEFAULT '',
    effect  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '{}',
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id DESC);

CREATE TABLE IF NOT EXISTS amendments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER,
    proposal   TEXT NOT NULL,
    rationale  TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER,
    layer      TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Provider/account availability. A subscription limit is an account fact, not a
-- task fact, so it is keyed by account and survives an orchestrator restart.
CREATE TABLE IF NOT EXISTS provider_state (
    account_key TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT 'default',
    status      TEXT NOT NULL DEFAULT 'AVAILABLE',
    reason      TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL DEFAULT '',
    retry_at    TEXT,
    raw_message TEXT NOT NULL DEFAULT '',
    consecutive INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gate_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    level      TEXT    NOT NULL,
    head_sha   TEXT    NOT NULL DEFAULT '',
    passed     INTEGER NOT NULL DEFAULT 0,
    summary    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    UNIQUE(task_id, level, head_sha)
);
"""

_JSON_FIELDS = ("owned_paths", "expected_write", "expected_read", "depends_on", "skills", "acceptance")

_WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 head_sha TEXT NOT NULL, verdict TEXT NOT NULL, reviewer TEXT NOT NULL,
 evidence TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PLANNING',
 revision INTEGER NOT NULL DEFAULT 1, planned_revision INTEGER NOT NULL DEFAULT 0,
 coordinator_session TEXT, next_check TEXT, last_error TEXT,
 completed_sha TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processes (
 id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, task_id INTEGER REFERENCES tasks(id),
 job_id TEXT, provider TEXT NOT NULL, account TEXT NOT NULL DEFAULT 'default',
 generation INTEGER NOT NULL DEFAULT 0, worker_run_id INTEGER,
 status TEXT NOT NULL DEFAULT 'STARTING', pid INTEGER, child_pid INTEGER,
 session_token TEXT, expected_head TEXT, launch_json TEXT NOT NULL,
 started_at TEXT NOT NULL, heartbeat_at TEXT, ended_at TEXT, exit_code INTEGER,
 result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_task_process ON processes(task_id)
 WHERE task_id IS NOT NULL AND status IN ('STARTING', 'RUNNING');
CREATE UNIQUE INDEX IF NOT EXISTS one_coordinator_process ON processes(job_id)
 WHERE purpose='coordinator' AND status IN ('STARTING', 'RUNNING');
CREATE TABLE IF NOT EXISTS quota_windows (
 account_key TEXT NOT NULL, bucket TEXT NOT NULL, window TEXT NOT NULL,
 used_percent REAL, resets_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(account_key, bucket, window)
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def db_path(root: str | Path) -> Path:
    return ai_dir(Path(root)) / "tasks.db"


BUSY_TIMEOUT_MS = 15_000
TX_RETRIES = 6


def connect_readonly(root: str | Path) -> sqlite3.Connection:
    """Read existing state without creating, migrating or configuring its journal."""
    path = db_path(root).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Read-only tools require an existing AgentKit database: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                           timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def connect(root: str | Path) -> sqlite3.Connection:
    """Open the runtime database in explicit-transaction mode.

    `isolation_level=None` turns off Python's implicit BEGIN so that
    `immediate_transaction` can take a write lock *before* reading — which is
    what makes lease acquisition atomic across processes (§1). Every existing
    single-statement write still commits on execute, so `conn.commit()` remains
    harmless.
    """
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for name, declaration in {"job_id": "TEXT", "skills": "TEXT NOT NULL DEFAULT '[]'",
                              "acceptance": "TEXT NOT NULL DEFAULT '[]'",
                              "complexity": "TEXT NOT NULL DEFAULT 'standard'", "model_profile": "TEXT"}.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
    conn.executescript(_WORKFLOW_SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS model_unavailable (provider TEXT, model TEXT, reason TEXT, retry_at TEXT, PRIMARY KEY(provider,model))")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_local_gpu_worker ON processes(provider) WHERE provider='local-opencode' AND status IN ('STARTING','RUNNING')")
    from . import schema_ext
    schema_ext.apply(conn)
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    # WAL readers must not acquire a writer lock just to open current state.
    if not version or version[0] != str(SCHEMA_VERSION):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    return conn


class LeaseConflict(Exception):
    """Raised inside a transaction to roll it back with the conflicting claims."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__("lease conflict")
        self.conflicts = conflicts


@contextmanager
def immediate_transaction(conn: sqlite3.Connection, *, retries: int = TX_RETRIES):
    """`BEGIN IMMEDIATE` … `COMMIT`, with bounded retry on writer contention.

    IMMEDIATE takes the write lock at BEGIN rather than at first write, so a
    check-then-insert sequence cannot interleave with another process's identical
    sequence. That is the whole mechanism behind "two overlapping exclusive
    leases are never both granted" — it does not depend on the advisory
    orchestrator lock, which may be absent or stale.
    """
    delay = 0.02
    for attempt in range(retries + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == retries:
                raise
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 1.0)
            continue

        try:
            yield conn
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if attempt == retries or "locked" not in str(exc).lower():
                    raise
                time.sleep(delay + random.random() * delay)
                delay = min(delay * 2, 1.0)
                continue
            return
    raise sqlite3.OperationalError("could not obtain a write transaction")


# --------------------------------------------------------------------- tasks


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    for key in _JSON_FIELDS:
        try:
            task[key] = json.loads(task.get(key) or "[]")
        except json.JSONDecodeError:
            task[key] = []
    return task


def _assignment_text(value: Any) -> str | None:
    """A named assignment stays a plain name; an inline policy is stored as JSON."""
    return value if value is None or isinstance(value, str) else json.dumps(value, sort_keys=True)


def create_task(conn: sqlite3.Connection, **fields: Any) -> int:
    now = utcnow()
    payload = {
        "spec_id": fields.get("spec_id"),
        "spec_hash": fields.get("spec_hash") or "",
        "title": fields.get("title") or "untitled",
        "description": fields.get("description") or "",
        "kind": fields.get("kind") or "SAFE_PARALLEL",
        "status": fields.get("status") or sm.PLANNED,
        "role": fields.get("role") or "implementer",
        "generation": int(fields.get("generation") or 0),
        "adapter": fields.get("adapter"),
        "model": fields.get("model"),
        "branch": fields.get("branch"),
        "worktree": fields.get("worktree"),
        "owned_paths": json.dumps(list(fields.get("owned_paths") or [])),
        "expected_write": json.dumps(list(fields.get("expected_write") or [])),
        "expected_read": json.dumps(list(fields.get("expected_read") or [])),
        "depends_on": json.dumps(list(fields.get("depends_on") or [])),
        "gate_level": fields.get("gate_level") or "fast",
        "priority": int(fields.get("priority") or 100),
        "budget_usd": fields.get("budget_usd"),
        "base_sha": fields.get("base_sha"),
        "next_action": fields.get("next_action"),
        "contract_version": fields.get("contract_version"),
        "job_id": fields.get("job_id"),
        "skills": json.dumps(fields.get("skills") or []),
        "acceptance": json.dumps(fields.get("acceptance") or []),
        "complexity": fields.get("complexity") or "standard",
        "model_profile": fields.get("model_profile"),
        "model_assignment": _assignment_text(fields.get("model_assignment")),
        "created_at": now,
        "updated_at": now,
    }
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{k}" for k in payload)
    cur = conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", payload)
    conn.commit()
    task_id = int(cur.lastrowid or 0)
    log_event(conn, task_id, "task_created", cause="spec", detail={"title": payload["title"]})
    return task_id


def upsert_task(conn: sqlite3.Connection, spec_id: str, **fields: Any) -> int:
    """Idempotent by `spec_id` (§14): re-running never duplicates a task."""
    row = conn.execute("SELECT id FROM tasks WHERE spec_id = ?", (spec_id,)).fetchone()
    if row is None:
        return create_task(conn, spec_id=spec_id, **fields)
    task_id = int(row["id"])
    update_task(conn, task_id, **fields)
    return task_id


def get_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def get_task_by_spec(conn: sqlite3.Connection, spec_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tasks WHERE spec_id = ?", (spec_id,)).fetchone()
    return _row_to_task(row) if row else None


def list_tasks(
    conn: sqlite3.Connection, statuses: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    if statuses:
        statuses = tuple(statuses)
        marks = ", ".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY priority, id", statuses
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY priority, id").fetchall()
    return [_row_to_task(r) for r in rows]


def update_task(conn: sqlite3.Connection, task_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments: list[str] = []
    params: dict[str, Any] = {"id": task_id, "updated_at": utcnow()}
    for key, value in fields.items():
        if key in _JSON_FIELDS and not isinstance(value, str):
            value = json.dumps(list(value or []))
        elif key == "model_assignment":
            value = _assignment_text(value)
        assignments.append(f"{key} = :{key}")
        params[key] = value
    conn.execute(
        f"UPDATE tasks SET {', '.join(assignments)}, updated_at = :updated_at WHERE id = :id",
        params,
    )


def set_status(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    *,
    actor: str = "scheduler",
    cause: str = "",
    evidence: str = "",
) -> None:
    """The only way a task changes state. Validates against the state machine."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} does not exist")
    current = str(task["status"])
    sm.validate(current, status, actor)
    if current == status:
        return
    update_task(conn, task_id, status=status)
    log_event(
        conn, task_id, "status_changed",
        cause=cause or f"{actor} requested {status}",
        effect=f"{current} -> {status}",
        detail={"actor": actor, "evidence": evidence[:2000]},
    )


def bump_generation(conn: sqlite3.Connection, task_id: int) -> int:
    """Invalidate every artefact of the previous attempt (§14).

    A zombie worker from an older generation that wakes up and calls back is
    rejected on the generation check, which removes a whole class of split-brain.
    """
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} does not exist")
    generation = int(task.get("generation") or 0) + 1
    update_task(conn, task_id, generation=generation)
    log_event(conn, task_id, "generation_bumped", effect=f"generation -> {generation}")
    return generation


def heartbeat(conn: sqlite3.Connection, task_id: int, generation: int | None = None) -> None:
    now = utcnow()
    update_task(conn, task_id, heartbeat_at=now)
    if generation is None:
        conn.execute(
            "UPDATE leases SET heartbeat_at = ? WHERE task_id = ? AND released_at IS NULL",
            (now, task_id),
        )
    else:
        conn.execute(
            "UPDATE leases SET heartbeat_at = ? "
            "WHERE task_id = ? AND generation = ? AND released_at IS NULL",
            (now, task_id, generation),
        )
    conn.commit()


def _is_externally_blocked(task: dict[str, Any]) -> bool:
    """True when something outside the task graph is holding this task."""
    raw = task.get("blocked_meta")
    if task.get("blocker"):
        return True
    if not raw:
        return False
    try:
        meta = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return True  # Unknown blocker metadata cannot authorize a paid retry.
    return bool(isinstance(meta, dict) and meta.get("reason"))


def refresh_ready(conn: sqlite3.Connection) -> list[int]:
    """Promote PLANNED tasks; BLOCKED needs explicit recovery authorization."""
    done_specs = {
        str(r["spec_id"]) for r in conn.execute(
            "SELECT spec_id FROM tasks WHERE status = 'DONE' AND spec_id IS NOT NULL"
        )
    }
    done_ids = {str(r["id"]) for r in conn.execute("SELECT id FROM tasks WHERE status = 'DONE'")}
    satisfied = done_specs | done_ids
    promoted: list[int] = []
    for task in list_tasks(conn, (sm.PLANNED,)):
        # Dependency completion cannot authorize retrying a worker failure.
        # BLOCKED tasks are released by explicit requeue or quota.wake_ready.
        if _is_externally_blocked(task):
            continue
        deps = {str(d) for d in task["depends_on"]}
        if deps.issubset(satisfied):
            try:
                set_status(conn, int(task["id"]), sm.READY,
                           cause="all dependencies DONE")
            except sm.TransitionError:
                continue
            promoted.append(int(task["id"]))
    return promoted


# -------------------------------------------------------------------- leases


def active_leases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Unreleased, unexpired leases belonging to a live task."""
    marks = ", ".join("?" for _ in sm.HOLDS_CLAIMS)
    rows = conn.execute(
        f"""
        SELECT l.*, t.status AS task_status, t.title AS task_title,
               t.adapter AS task_adapter, t.generation AS task_generation
        FROM leases l JOIN tasks t ON t.id = l.task_id
        WHERE l.released_at IS NULL AND t.status IN ({marks})
        ORDER BY l.id
        """,
        tuple(sm.HOLDS_CLAIMS),
    ).fetchall()
    now = datetime.now(UTC)
    live: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        if is_lease_expired(record, now):
            continue
        live.append(record)
    return live


def is_lease_expired(lease: dict[str, Any], now: datetime | None = None) -> bool:
    """Expired by hard ceiling, or by missed heartbeat (§6.2)."""
    now = now or datetime.now(UTC)
    hard = parse_ts(lease.get("expires_at"))
    if hard and hard < now:
        return True
    ttl = int(lease.get("ttl_seconds") or 0)
    if ttl <= 0:
        return False
    beat = parse_ts(lease.get("heartbeat_at")) or parse_ts(lease.get("acquired_at"))
    if beat is None:
        return False
    return (now - beat).total_seconds() > ttl


def acquire_leases(
    conn: sqlite3.Connection,
    task_id: int,
    globs: Iterable[str],
    *,
    mode: str = "exclusive-write",
    ttl_seconds: int = 600,
    max_hours: int = 4,
    generation: int | None = None,
    check_conflicts: bool = True,
) -> list[str]:
    """Grant leases atomically, or raise `LeaseConflict` having changed nothing.

    The conflict evaluation and the INSERTs happen inside one `BEGIN IMMEDIATE`
    transaction (§1). Two processes running this concurrently serialise: the
    second sees the first's rows and rolls back. A check performed before the
    transaction would let both observe "no conflict" and both insert.

    `check_conflicts=False` exists only for tests that deliberately construct
    overlapping state; production callers must never pass it.
    """
    if mode not in LEASE_MODES:
        raise ValueError(f"unknown lease mode {mode!r}")
    requested = [str(g) for g in globs if str(g).strip()]
    if not requested:
        return []

    added: list[str] = []
    with immediate_transaction(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"task {task_id} does not exist")
        task = _row_to_task(row)
        gen = int(task.get("generation") or 0) if generation is None else generation

        if check_conflicts:
            clashes = _conflicts_in_tx(conn, requested, task_id, mode)
            if clashes:
                raise LeaseConflict(clashes)

        now = datetime.now(UTC)
        stamp = now.isoformat(timespec="seconds")
        expires = (now + timedelta(hours=max_hours)).isoformat(timespec="seconds")
        for glob in requested:
            conn.execute(
                "INSERT INTO leases (task_id, generation, path_glob, mode, acquired_at, "
                "expires_at, heartbeat_at, ttl_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, gen, glob, mode, stamp, expires, stamp, ttl_seconds),
            )
            added.append(glob)

    log_event(conn, task_id, "leases_acquired",
              cause=f"mode={mode} generation={generation}",
              detail={"globs": added})
    return added


def try_acquire_leases(
    conn: sqlite3.Connection, task_id: int, globs: Iterable[str], **kwargs: Any
) -> tuple[list[str], list[dict[str, Any]]]:
    """`acquire_leases` without the exception. Returns (granted, conflicts)."""
    try:
        return acquire_leases(conn, task_id, globs, **kwargs), []
    except LeaseConflict as conflict:
        return [], conflict.conflicts


def _conflicts_in_tx(
    conn: sqlite3.Connection, requested: list[str], task_id: int, mode: str
) -> list[dict[str, Any]]:
    """Overlap evaluation that runs *inside* the write transaction.

    Deliberately self-contained: importing the higher-level `leases` module here
    would create a cycle, and this must read through the same connection that
    holds the write lock.
    """
    from . import globs as globlib
    from . import statemachine as sm

    now = datetime.now(UTC)
    marks = ", ".join("?" for _ in sm.HOLDS_CLAIMS)
    rows = conn.execute(
        f"""SELECT l.*, t.title AS task_title, t.status AS task_status
            FROM leases l JOIN tasks t ON t.id = l.task_id
            WHERE l.released_at IS NULL AND t.status IN ({marks}) AND l.task_id != ?""",
        (*sm.HOLDS_CLAIMS, task_id),
    ).fetchall()

    found: list[dict[str, Any]] = []
    for row in rows:
        held = dict(row)
        if is_lease_expired(held, now):
            continue
        # Two shared-read leases never conflict; anything involving a writer does.
        if str(held.get("mode")) == "shared-read" and mode == "shared-read":
            continue
        for pattern in requested:
            if globlib.overlaps(str(held["path_glob"]), pattern):
                found.append({
                    "task_id": int(held["task_id"]),
                    "task_title": held["task_title"],
                    "held": held["path_glob"],
                    "held_mode": held["mode"],
                    "requested": pattern,
                    "source": "lease",
                })
                break

    # Tasks that are live and scoped but have not yet taken an explicit lease.
    task_rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({marks}) AND id != ?",
        (*sm.HOLDS_CLAIMS, task_id),
    ).fetchall()
    already = {(c["task_id"], c["held"]) for c in found}
    for row in task_rows:
        other = _row_to_task(row)
        for owned in other.get("owned_paths") or []:
            if (int(other["id"]), str(owned)) in already:
                continue
            for pattern in requested:
                if globlib.overlaps(str(owned), pattern):
                    found.append({
                        "task_id": int(other["id"]),
                        "task_title": other["title"],
                        "held": str(owned),
                        "held_mode": "exclusive-write",
                        "requested": pattern,
                        "source": "owned_paths",
                    })
                    break
    return found


def release_leases(
    conn: sqlite3.Connection, task_id: int, reason: str = "task finished"
) -> int:
    cur = conn.execute(
        "UPDATE leases SET released_at = ?, released_reason = ? "
        "WHERE task_id = ? AND released_at IS NULL",
        (utcnow(), reason, task_id),
    )
    conn.commit()
    if cur.rowcount:
        log_event(conn, task_id, "leases_released", cause=reason,
                  detail={"count": cur.rowcount})
    return int(cur.rowcount)


def expire_stale_leases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Mark missed-heartbeat leases expired. Never kills the owning process."""
    now = datetime.now(UTC)
    # Only *running* tasks can miss a heartbeat. A BLOCKED task is paused on
    # purpose — expiring its lease would hand its hotspot to someone else while
    # it waits for a provider to come back.
    states = (sm.LEASED, sm.RUNNING, sm.VERIFYING)
    marks = ", ".join("?" for _ in states)
    rows = conn.execute(
        f"""SELECT l.*, t.status AS task_status, t.title AS task_title
            FROM leases l JOIN tasks t ON t.id = l.task_id
            WHERE l.released_at IS NULL AND t.status IN ({marks})""",
        states,
    ).fetchall()
    expired: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        if not is_lease_expired(record, now):
            continue
        beat = record.get("heartbeat_at") or record.get("acquired_at")
        conn.execute(
            "UPDATE leases SET released_at = ?, released_reason = ? WHERE id = ?",
            (utcnow(), f"expired: no heartbeat since {beat}", record["id"]),
        )
        expired.append(record)
    conn.commit()
    return expired


# ---------------------------------------------------------------- worker runs


def open_worker_run(
    conn: sqlite3.Connection, task_id: int, generation: int, adapter: str, worktree: str = ""
) -> int | None:
    """Claim the right to launch. Returns None if this generation already ran.

    This is the idempotency key from §14: inserted *before* the process spawns,
    so a second concurrent `agentkit run` loses the insert and aborts.
    """
    try:
        cur = conn.execute(
            "INSERT INTO worker_runs (task_id, generation, adapter, worktree, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, generation, adapter, worktree, utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        return None


def close_worker_run(
    conn: sqlite3.Connection, run_id: int, exit_code: int | None, pid: int | None = None
) -> None:
    conn.execute(
        "UPDATE worker_runs SET ended_at = ?, exit_code = ?, pid = COALESCE(?, pid) "
        "WHERE id = ?",
        (utcnow(), exit_code, pid, run_id),
    )
    conn.commit()


def set_worker_pid(conn: sqlite3.Connection, run_id: int, pid: int) -> None:
    conn.execute("UPDATE worker_runs SET pid = ? WHERE id = ?", (pid, run_id))
    conn.commit()


def latest_worker_run(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM worker_runs WHERE task_id = ? ORDER BY generation DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------- checkpoints


def write_checkpoint(
    conn: sqlite3.Connection,
    task_id: int,
    payload: dict[str, Any],
    *,
    kind: str = "mechanical",
    reason: str = "manual",
    head_sha: str = "",
    generation: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO checkpoints (task_id, kind, reason, head_sha, generation, payload, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, kind, reason, head_sha, generation,
         json.dumps(redact(payload), ensure_ascii=False, default=str), utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def latest_checkpoint(
    conn: sqlite3.Connection, task_id: int, kind: str | None = None
) -> dict[str, Any] | None:
    if kind:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["payload"] = json.loads(data["payload"])
    except json.JSONDecodeError:
        data["payload"] = {}
    return data


# -------------------------------------------------------------------- events


def log_event(
    conn: sqlite3.Connection,
    task_id: int | None,
    kind: str,
    *,
    cause: str = "",
    effect: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    # Redaction happens here, once, rather than at every call site. Anything
    # credential-shaped is stripped before it can reach the event log (§5).
    conn.execute(
        "INSERT INTO events (task_id, kind, cause, effect, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, kind, redact(cause), redact(effect),
         json.dumps(redact(detail or {}), ensure_ascii=False, default=str), utcnow()),
    )


def recent_events(
    conn: sqlite3.Connection,
    task_id: int | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        try:
            record["detail"] = json.loads(record["detail"])
        except json.JSONDecodeError:
            record["detail"] = {}
        out.append(record)
    return out


# ---------------------------------------------------------- violations, gates


def record_violation(
    conn: sqlite3.Connection, task_id: int | None, layer: str, path: str,
    reason: str = "", channel: str = "",
) -> None:
    conn.execute(
        "INSERT INTO violations (task_id, layer, channel, path, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, layer, channel, path, redact(reason), utcnow()),
    )
    conn.commit()
    log_event(conn, task_id, "lease_violation", cause=f"{layer} {channel}".strip(),
              effect="recorded", detail={"path": path, "reason": reason})


def list_violations(conn: sqlite3.Connection, task_id: int | None = None) -> list[dict[str, Any]]:
    if task_id is None:
        rows = conn.execute("SELECT * FROM violations ORDER BY id DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM violations WHERE task_id = ? ORDER BY id DESC", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_gate(
    conn: sqlite3.Connection, task_id: int, level: str, head_sha: str,
    passed: bool, summary: str,
) -> None:
    conn.execute(
        "INSERT INTO gate_results (task_id, level, head_sha, passed, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, level, head_sha) DO UPDATE SET "
        "passed = excluded.passed, summary = excluded.summary, created_at = excluded.created_at",
        (task_id, level, head_sha, 1 if passed else 0, redact(summary)[:8000], utcnow()),
    )
    conn.commit()


def cached_gate(
    conn: sqlite3.Connection, task_id: int, level: str, head_sha: str
) -> dict[str, Any] | None:
    """A gate result is valid only for the commit it ran against (§14)."""
    row = conn.execute(
        "SELECT * FROM gate_results WHERE task_id = ? AND level = ? AND head_sha = ?",
        (task_id, level, head_sha),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- amendments


def create_amendment(
    conn: sqlite3.Connection, task_id: int | None, proposal: str, rationale: str = ""
) -> int:
    cur = conn.execute(
        "INSERT INTO amendments (task_id, proposal, rationale, created_at) VALUES (?, ?, ?, ?)",
        (task_id, redact(proposal), redact(rationale), utcnow()),
    )
    conn.commit()
    log_event(conn, task_id, "amendment_proposed", cause=rationale,
              detail={"proposal": proposal})
    return int(cur.lastrowid or 0)


def list_amendments(conn: sqlite3.Connection, status: str = "OPEN") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM amendments WHERE status = ? ORDER BY id DESC", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_amendment(conn: sqlite3.Connection, amendment_id: int, status: str) -> None:
    conn.execute("UPDATE amendments SET status = ? WHERE id = ?", (status, amendment_id))
    conn.commit()
