"""Acceptance test 24: crashes at SQLite transactional boundaries.

Runtime safety now leans hard on SQLite, so the interesting question is not "does
a transaction work" but "what is left behind when the process dies mid-way". Each
test kills a **real** child process at a specific point and then asserts what
reconcile sees.

The four states that must never exist afterwards:

    two active leases on overlapping paths
    two active workers for the same generation
    a worker whose ownership cannot be reconstructed
    a task advanced to RUNNING that never actually started

These are achieved with transactions and idempotency keys, not with repair
heuristics that guess at intent.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentkit import db, reconcile
from agentkit import statemachine as sm

CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from agentkit import db

    root, _pkg, task_id, stage = sys.argv[2], sys.argv[1], int(sys.argv[3]), sys.argv[4]
    conn = db.connect(root)

    def die():
        # os._exit skips every cleanup path, which is what a real kill looks like.
        os._exit(9)

    if stage == "after_check_before_insert":
        with db.immediate_transaction(conn):
            db._conflicts_in_tx(conn, ["services/**"], task_id, "exclusive-write")
            die()

    elif stage == "after_insert_before_commit":
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO leases (task_id, generation, path_glob, mode, acquired_at,"
            " expires_at, heartbeat_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, 1, "services/**", "exclusive-write", db.utcnow(),
             None, db.utcnow(), 600),
        )
        die()

    elif stage == "after_commit_before_spawn":
        db.acquire_leases(conn, task_id, ["services/**"])
        db.open_worker_run(conn, task_id, 1, "test-adapter", str(root))
        die()

    elif stage == "after_run_row_before_status":
        db.acquire_leases(conn, task_id, ["services/**"])
        db.open_worker_run(conn, task_id, 1, "test-adapter", str(root))
        db.set_status(conn, task_id, "LEASED", actor="scheduler")
        die()

    print("did not crash", file=sys.stderr)
    sys.exit(1)
    """
)


@pytest.fixture()
def crash_script(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("crash") / "crash.py"
    path.write_text(CRASH_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture()
def crash_repo(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text("name: crash\n", encoding="utf-8")
    (tmp_path / ".ai" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    conn = db.connect(tmp_path)
    try:
        task_id = db.create_task(conn, spec_id="a", title="task a", status=sm.READY)
    finally:
        conn.close()
    return tmp_path, task_id


def _crash_at(root: Path, task_id: int, stage: str, script: Path) -> int:
    package_dir = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, str(script), package_dir, str(root), str(task_id), stage],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 1, f"process should have been killed: {proc.stderr[:400]}"
    return proc.returncode


class TestCrashBoundaries:
    def test_crash_after_conflict_check_leaves_no_lease(self, crash_repo, crash_script):
        """The transaction never committed, so nothing was granted."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_check_before_insert", crash_script)

        conn = db.connect(root)
        try:
            assert db.active_leases(conn) == []
        finally:
            conn.close()

    def test_crash_after_insert_before_commit_leaves_no_lease(self, crash_repo, crash_script):
        """An uncommitted INSERT must not survive. This is the dangerous one."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_insert_before_commit", crash_script)

        conn = db.connect(root)
        try:
            assert db.active_leases(conn) == [], "an uncommitted lease became visible"
        finally:
            conn.close()

    def test_a_later_acquirer_still_succeeds_after_that_crash(self, crash_repo, crash_script):
        """A crashed grant must not poison the path for everyone else."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_insert_before_commit", crash_script)

        conn = db.connect(root)
        try:
            other = db.create_task(conn, spec_id="b", title="task b", status=sm.RUNNING)
            granted = db.acquire_leases(conn, other, ["services/**"])
            assert granted == ["services/**"]
        finally:
            conn.close()

    def test_crash_after_commit_leaves_exactly_one_lease(self, crash_repo, crash_script):
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_commit_before_spawn", crash_script)

        conn = db.connect(root)
        try:
            assert len(db.active_leases(conn)) == 1
            run = db.latest_worker_run(conn, task_id)
            assert run is not None and run["generation"] == 1
        finally:
            conn.close()

    def test_generation_cannot_be_double_claimed_after_a_crash(self, crash_repo, crash_script):
        """The idempotency key survives the crash, so no twin is ever launched."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_commit_before_spawn", crash_script)

        conn = db.connect(root)
        try:
            assert db.open_worker_run(conn, task_id, 1, "test-adapter") is None
        finally:
            conn.close()

    def test_task_is_not_left_running_without_a_worker(self, crash_repo, crash_script):
        """A task whose worker never started must not look alive after reconcile."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_run_row_before_status", crash_script)

        reconcile.reconcile(root)
        conn = db.connect(root)
        try:
            task = db.get_task(conn, task_id)
            assert task["status"] in (sm.STALE, sm.READY, sm.PLANNED, sm.NEEDS_REPLAN)
            assert task["status"] != sm.RUNNING
        finally:
            conn.close()

    def test_reconcile_releases_leases_of_dead_workers(self, crash_repo, crash_script):
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_run_row_before_status", crash_script)

        reconcile.reconcile(root)
        conn = db.connect(root)
        try:
            assert db.active_leases(conn) == [], (
                "a dead worker's lease must not keep blocking the path"
            )
        finally:
            conn.close()

    def test_ownership_is_reconstructable_after_a_crash(self, crash_repo, crash_script):
        """Every surviving worker_run must still be attributable to a task."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_run_row_before_status", crash_script)

        conn = db.connect(root)
        try:
            run = db.latest_worker_run(conn, task_id)
            assert run is not None
            assert db.get_task(conn, int(run["task_id"])) is not None
        finally:
            conn.close()

    def test_reconcile_is_idempotent_after_a_crash(self, crash_repo, crash_script):
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_commit_before_spawn", crash_script)

        reconcile.reconcile(root)
        second = reconcile.reconcile(root)

        conn = db.connect(root)
        try:
            assert len(db.list_tasks(conn)) == 1
            assert len(db.active_leases(conn)) <= 1
        finally:
            conn.close()
        assert second.created == []


class TestDatabaseDurability:
    def test_database_survives_and_reopens(self, crash_repo, crash_script):
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_insert_before_commit", crash_script)
        conn = db.connect(root)
        try:
            assert db.get_task(conn, task_id) is not None
        finally:
            conn.close()

    def test_rebuild_from_git_grants_nothing(self, crash_repo, crash_script):
        """Even after a crash, a rebuilt database invents no authority."""
        root, task_id = crash_repo
        _crash_at(root, task_id, "after_commit_before_spawn", crash_script)
        db.db_path(root).unlink()

        reconcile.rebuild_from_git(root)
        conn = db.connect(root)
        try:
            assert db.active_leases(conn) == []
        finally:
            conn.close()
