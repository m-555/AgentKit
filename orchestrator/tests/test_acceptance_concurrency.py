"""Acceptance test 15: two processes racing for overlapping leases.

The invariant: two overlapping exclusive-write leases are never both granted,
even when two orchestrators attempt the grants at the same instant.

A `check conflicts` / `INSERT` pair cannot provide this — both processes can
observe "no conflict" before either insert commits. These tests use **real
separate processes** against one database file, repeatedly, because a race that
only loses one time in fifty will not show up in a single run.

Correctness here must not depend on the advisory orchestrator lock, so nothing in
this module takes one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentkit import db

RACE_SCRIPT = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, sys.argv[4])
    from agentkit import db

    root, task_id, glob, _pkg, barrier = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]

    conn = db.connect(root)
    # Spin until the shared start time, so both processes contend on the same moment.
    target = float(barrier)
    while time.time() < target:
        pass

    try:
        granted = db.acquire_leases(conn, task_id, [glob])
        print(json.dumps({"result": "granted", "globs": granted}))
    except db.LeaseConflict as conflict:
        print(json.dumps({"result": "conflict", "conflicts": conflict.conflicts}))
    except Exception as exc:            # any other failure is a test failure
        print(json.dumps({"result": "error", "error": f"{type(exc).__name__}: {exc}"}))
    finally:
        conn.close()
    """
)


@pytest.fixture()
def race_repo(tmp_path: Path):
    """A managed repo with two live tasks whose paths overlap."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text("name: race\n", encoding="utf-8")
    conn = db.connect(tmp_path)
    try:
        a = db.create_task(conn, spec_id="a", title="task a", status="RUNNING")
        b = db.create_task(conn, spec_id="b", title="task b", status="RUNNING")
    finally:
        conn.close()
    return tmp_path, a, b


def _run_race(root: Path, pairs: list[tuple[int, str]], script_path: Path) -> list[dict]:
    import time

    package_dir = str(Path(__file__).resolve().parents[1])
    start_at = time.time() + 0.45
    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path), str(root), str(task_id), glob,
             package_dir, f"{start_at:.6f}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for task_id, glob in pairs
    ]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        line = (out or "").strip().splitlines()
        assert line, f"race process produced no output; stderr={err[:600]}"
        results.append(json.loads(line[-1]))
    return results


@pytest.fixture()
def script(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("race") / "race.py"
    path.write_text(RACE_SCRIPT, encoding="utf-8")
    return path


class TestConcurrentLeaseAcquisition:
    @pytest.mark.parametrize("iteration", range(12))
    def test_parent_and_child_glob_race(self, tmp_path_factory, script, iteration):
        """`services/**` versus `services/media/**` — exactly one must win."""
        root = tmp_path_factory.mktemp(f"race-nested-{iteration}")
        (root / ".ai").mkdir()
        (root / ".ai" / "project.yaml").write_text("name: race\n", encoding="utf-8")
        conn = db.connect(root)
        try:
            a = db.create_task(conn, spec_id="a", title="task a", status="RUNNING")
            b = db.create_task(conn, spec_id="b", title="task b", status="RUNNING")
        finally:
            conn.close()

        results = _run_race(root, [(a, "services/**"), (b, "services/media/**")], script)

        granted = [r for r in results if r["result"] == "granted"]
        conflicts = [r for r in results if r["result"] == "conflict"]
        errors = [r for r in results if r["result"] == "error"]

        assert not errors, f"unexpected errors: {errors}"
        assert len(granted) == 1, f"expected exactly one grant, got {results}"
        assert len(conflicts) == 1, f"expected exactly one conflict, got {results}"

        conn = db.connect(root)
        try:
            active = db.active_leases(conn)
            assert len(active) == 1, f"expected 1 active lease, found {len(active)}"
        finally:
            conn.close()

    @pytest.mark.parametrize("iteration", range(8))
    def test_exact_path_collision_race(self, tmp_path_factory, script, iteration):
        root = tmp_path_factory.mktemp(f"race-exact-{iteration}")
        (root / ".ai").mkdir()
        (root / ".ai" / "project.yaml").write_text("name: race\n", encoding="utf-8")
        conn = db.connect(root)
        try:
            a = db.create_task(conn, spec_id="a", title="task a", status="RUNNING")
            b = db.create_task(conn, spec_id="b", title="task b", status="RUNNING")
        finally:
            conn.close()

        results = _run_race(
            root, [(a, "services/media.py"), (b, "services/media.py")], script
        )
        assert sum(r["result"] == "granted" for r in results) == 1, results

        conn = db.connect(root)
        try:
            assert len(db.active_leases(conn)) == 1
        finally:
            conn.close()

    @pytest.mark.parametrize("iteration", range(6))
    def test_disjoint_paths_both_succeed(self, tmp_path_factory, script, iteration):
        """The check must not be so coarse that it serialises unrelated work."""
        root = tmp_path_factory.mktemp(f"race-disjoint-{iteration}")
        (root / ".ai").mkdir()
        (root / ".ai" / "project.yaml").write_text("name: race\n", encoding="utf-8")
        conn = db.connect(root)
        try:
            a = db.create_task(conn, spec_id="a", title="task a", status="RUNNING")
            b = db.create_task(conn, spec_id="b", title="task b", status="RUNNING")
        finally:
            conn.close()

        results = _run_race(root, [(a, "services/a/**"), (b, "services/b/**")], script)
        assert all(r["result"] == "granted" for r in results), results

        conn = db.connect(root)
        try:
            assert len(db.active_leases(conn)) == 2
        finally:
            conn.close()


class TestLeaseSemantics:
    def test_failed_acquisition_mutates_nothing(self, race_repo):
        root, a, b = race_repo
        conn = db.connect(root)
        try:
            db.acquire_leases(conn, a, ["services/**"])
            before = len(db.active_leases(conn))
            events_before = len(db.recent_events(conn, limit=200))

            with pytest.raises(db.LeaseConflict):
                db.acquire_leases(conn, b, ["services/media/**"])

            assert len(db.active_leases(conn)) == before
            # A rolled-back grant must not leave a "leases_acquired" event behind.
            kinds = [e["kind"] for e in db.recent_events(conn, task_id=b, limit=50)]
            assert "leases_acquired" not in kinds
            assert len(db.recent_events(conn, limit=200)) >= events_before
        finally:
            conn.close()

    def test_two_shared_reads_coexist(self, race_repo):
        root, a, b = race_repo
        conn = db.connect(root)
        try:
            db.acquire_leases(conn, a, ["contracts/**"], mode="shared-read")
            db.acquire_leases(conn, b, ["contracts/**"], mode="shared-read")
            assert len(db.active_leases(conn)) == 2
        finally:
            conn.close()

    def test_writer_blocks_reader_and_reader_blocks_writer(self, race_repo):
        root, a, b = race_repo
        conn = db.connect(root)
        try:
            db.acquire_leases(conn, a, ["contracts/**"], mode="exclusive-write")
            with pytest.raises(db.LeaseConflict):
                db.acquire_leases(conn, b, ["contracts/api.yaml"], mode="shared-read")
        finally:
            conn.close()

        conn = db.connect(root)
        try:
            db.release_leases(conn, a)
            db.acquire_leases(conn, b, ["contracts/**"], mode="shared-read")
            with pytest.raises(db.LeaseConflict):
                db.acquire_leases(conn, a, ["contracts/api.yaml"], mode="exclusive-write")
        finally:
            conn.close()

    def test_same_task_may_extend_its_own_lease(self, race_repo):
        root, a, _b = race_repo
        conn = db.connect(root)
        try:
            db.acquire_leases(conn, a, ["services/**"])
            db.acquire_leases(conn, a, ["services/media/**"])
            assert len(db.active_leases(conn)) == 2
        finally:
            conn.close()

    def test_owned_paths_of_a_live_task_also_block(self, race_repo):
        """A planned-but-unleased task is protected too, or planning is a gap."""
        root, a, b = race_repo
        conn = db.connect(root)
        try:
            db.update_task(conn, a, owned_paths=["services/media.py"])
            with pytest.raises(db.LeaseConflict) as caught:
                db.acquire_leases(conn, b, ["services/**"])
            assert caught.value.conflicts[0]["source"] == "owned_paths"
        finally:
            conn.close()
