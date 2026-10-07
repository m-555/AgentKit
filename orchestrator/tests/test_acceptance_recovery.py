"""Acceptance tests 4-7 and 11: recovery, restart safety and idempotency.

The property under test throughout: killing any component at any instant leaves
recoverable state and never duplicates work. Invariant 6 is the sharp edge —
recovery must never invent authority, because a guessed lease is the one mistake
that puts two agents in one file.
"""

from __future__ import annotations

from pathlib import Path

from agentkit import checkpoints, db, reconcile, recovery, spec
from agentkit import statemachine as sm
from tests.conftest import commit_all, git


class TestCrashRecovery:
    """Test 4 — a worker dies mid-task."""

    def test_mechanical_checkpoint_survives_without_the_model(
        self, conn, project_root, make_task
    ):
        task = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        db.update_task(conn, task, base_sha=base)

        (project_root / "services" / "retry.py").write_text("work\n", encoding="utf-8")
        commit_all(project_root, "partial work")

        snapshot = checkpoints.mechanical_snapshot(conn, project_root, project_root, task)
        assert snapshot["commits_since_start"]
        assert snapshot["head_sha"]
        assert snapshot["base_sha"] == base

    def test_recovery_works_with_no_semantic_checkpoint(
        self, conn, project_root, make_task
    ):
        """The whole point of the two-layer split: step 4 always produces a brief."""
        task = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        db.update_task(conn, task, base_sha=base)
        (project_root / "services" / "retry.py").write_text("work\n", encoding="utf-8")
        commit_all(project_root, "partial work")

        packet = checkpoints.recover(conn, project_root, project_root, task)
        assert packet["reconstructed"] is True
        assert packet["semantic"]["next_action"]
        assert packet["semantic"]["completed"]           # derived from commit subjects

    def test_stale_semantic_checkpoint_keeps_decisions_drops_progress(
        self, conn, project_root, make_task
    ):
        task = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        db.update_task(conn, task, base_sha=base)

        checkpoints.write_semantic(
            conn, task,
            {"completed": ["everything, honestly"], "next_action": "ship it",
             "decisions": ["normalise provider results"]},
            head_sha="deadbeef",
        )
        (project_root / "services" / "retry.py").write_text("more\n", encoding="utf-8")
        commit_all(project_root, "later work")

        packet = checkpoints.recover(conn, project_root, project_root, task)
        assert "normalise provider results" in packet["semantic"]["decisions"]
        assert "everything, honestly" not in (packet["semantic"].get("completed") or [])
        assert any("earlier commit" in w for w in packet["warnings"])

    def test_crash_relaunches_until_the_limit_then_replans(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        for _ in range(recovery.MAX_ATTEMPTS - 1):
            task = db.get_task(conn, task_id)
            action = recovery.decide(conn, task, recovery.Failure.CRASH)
            assert action.relaunch
            recovery.apply(conn, task_id, action)
            db.set_status(conn, task_id, sm.LEASED, actor="scheduler")
            db.set_status(conn, task_id, sm.RUNNING, actor="scheduler")

        task = db.get_task(conn, task_id)
        action = recovery.decide(conn, task, recovery.Failure.CRASH)
        assert not action.relaunch
        assert action.next_status == sm.NEEDS_REPLAN
        assert action.escalate == "architect"

    def test_provider_outage_does_not_consume_an_attempt(self, conn, make_task):
        """The rule most systems get wrong (§13, class 9)."""
        task_id = make_task("add retry", ["services/retry.py"])
        before = int(db.get_task(conn, task_id)["attempts"])

        action = recovery.decide(
            conn, db.get_task(conn, task_id), recovery.Failure.PROVIDER_UNAVAILABLE
        )
        recovery.apply(conn, task_id, action)

        assert action.relaunch
        assert not action.consumes_attempt
        assert int(db.get_task(conn, task_id)["attempts"]) == before

    def test_rate_limit_is_classified_as_a_provider_problem(self):
        assert recovery.classify_exit(1, "Error: 429 rate limit exceeded") == \
            recovery.Failure.PROVIDER_UNAVAILABLE
        assert recovery.classify_exit(1, "boom") == recovery.Failure.CRASH


class TestStaleLease:
    """Test 11 — a lease expires while the process may still be alive."""

    def test_expired_lease_stops_blocking_others(self, conn, project, make_task):
        from agentkit.leases import decide

        owner = make_task("split media", [])
        db.acquire_leases(conn, owner, ["services/media.py"], ttl_seconds=0)
        conn.execute(
            "UPDATE leases SET heartbeat_at = '2020-01-01T00:00:00+00:00', ttl_seconds = 60 "
            "WHERE task_id = ?", (owner,),
        )
        conn.commit()

        intruder = make_task("other", [])
        assert decide(conn, project, "services/media.py", intruder).allowed

    def test_expiry_preserves_the_worktree_and_never_kills(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        db.update_task(conn, task_id, worktree="/tmp/wt-retry")

        action = recovery.decide(
            conn, db.get_task(conn, task_id), recovery.Failure.STALLED
        )
        recovery.apply(conn, task_id, action)

        task = db.get_task(conn, task_id)
        assert task["status"] == sm.STALE
        assert task["worktree"] == "/tmp/wt-retry"
        assert action.escalate == "human"

    def test_adopt_bumps_the_generation(self, conn, make_task):
        """Invalidates anything the previous worker might still do (§14)."""
        task_id = make_task("add retry", ["services/retry.py"], status="RUNNING")
        recovery.apply(conn, task_id,
                       recovery.decide(conn, db.get_task(conn, task_id),
                                       recovery.Failure.STALLED))
        before = int(db.get_task(conn, task_id)["generation"])

        recovery.adopt(conn, task_id)
        task = db.get_task(conn, task_id)
        assert task["status"] == sm.READY
        assert int(task["generation"]) == before + 1


class TestOrchestratorRestart:
    """Tests 6 and 7 — reconcile and double-run."""

    def _write_spec(self, project_root: Path, ids: list[str]) -> None:
        specs = [
            spec.TaskSpec(spec_id=i, title=f"task {i}", expected_write=[f"services/{i}.py"])
            for i in ids
        ]
        spec.save(project_root, specs)

    def test_reconcile_creates_tasks_from_the_spec(self, project_root):
        self._write_spec(project_root, ["alpha", "beta"])
        report = reconcile.reconcile(project_root)
        assert sorted(report.created) == ["alpha", "beta"]

    def test_reconcile_is_idempotent(self, project_root):
        """Test 7: running twice must not duplicate anything."""
        self._write_spec(project_root, ["alpha", "beta"])
        reconcile.reconcile(project_root)
        second = reconcile.reconcile(project_root)

        assert second.created == []
        conn = db.connect(project_root)
        try:
            assert len(db.list_tasks(conn)) == 2
        finally:
            conn.close()

    def test_editing_the_spec_of_an_in_flight_task_forces_replan(self, project_root):
        self._write_spec(project_root, ["alpha"])
        reconcile.reconcile(project_root)

        conn = db.connect(project_root)
        try:
            task = db.get_task_by_spec(conn, "alpha")
            db.set_status(conn, int(task["id"]), sm.READY, actor="scheduler")
            db.set_status(conn, int(task["id"]), sm.LEASED, actor="scheduler")
            db.set_status(conn, int(task["id"]), sm.RUNNING, actor="scheduler")
        finally:
            conn.close()

        spec.save(project_root, [
            spec.TaskSpec(spec_id="alpha", title="task alpha",
                          expected_write=["services/alpha.py", "services/extra.py"]),
        ])
        report = reconcile.reconcile(project_root)

        assert "alpha" in report.replan
        conn = db.connect(project_root)
        try:
            assert db.get_task_by_spec(conn, "alpha")["status"] == sm.NEEDS_REPLAN
        finally:
            conn.close()

    def test_rebuilding_after_database_loss_grants_no_leases(self, project_root):
        """Invariant 6: recovery never invents authority."""
        self._write_spec(project_root, ["alpha"])
        reconcile.reconcile(project_root)
        db.db_path(project_root).unlink()

        report = reconcile.rebuild_from_git(project_root)

        conn = db.connect(project_root)
        try:
            assert db.active_leases(conn) == []
            assert db.get_task_by_spec(conn, "alpha") is not None
        finally:
            conn.close()
        assert any("no leases were granted" in n for n in report.notes)

    def test_worker_run_claim_is_unique_per_generation(self, conn, make_task):
        """The idempotency key that makes a double launch impossible."""
        task_id = make_task("add retry", ["services/retry.py"])
        first = db.open_worker_run(conn, task_id, 1, "claude-code")
        second = db.open_worker_run(conn, task_id, 1, "claude-code")

        assert first is not None
        assert second is None, "a second launch at the same generation must be refused"

        assert db.open_worker_run(conn, task_id, 2, "claude-code") is not None

    def test_dependency_cycles_are_rejected(self, project_root):
        try:
            spec.save(project_root, [
                spec.TaskSpec(spec_id="a", title="a", depends_on=["b"]),
                spec.TaskSpec(spec_id="b", title="b", depends_on=["a"]),
            ])
        except ValueError as exc:
            assert "cycle" in str(exc)
        else:
            raise AssertionError("a dependency cycle should not load")


class TestContextLoss:
    """Test 5 — session history deleted entirely."""

    def test_brief_is_reconstructed_from_git_alone(self, conn, project_root, make_task):
        task = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        db.update_task(conn, task, base_sha=base)
        (project_root / "services" / "retry.py").write_text("half done\n", encoding="utf-8")
        commit_all(project_root, "add retry skeleton")

        # No checkpoints of any kind: the worst case.
        conn.execute("DELETE FROM checkpoints")
        conn.commit()

        packet = checkpoints.recover(conn, project_root, project_root, task)
        rendered = checkpoints.render_recovery(packet)

        assert packet["reconstructed"]
        assert "add retry skeleton" in rendered
        assert "Reconstructed" in rendered

    def test_worktree_drift_is_reported_and_git_wins(self, conn, project_root, make_task):
        task = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        db.update_task(conn, task, base_sha=base)
        db.write_checkpoint(
            conn, task, {"head_sha": "0000000", "base_sha": base}, kind="mechanical",
            head_sha="0000000",
        )

        packet = checkpoints.recover(conn, project_root, project_root, task)
        assert any("drift" in w for w in packet["warnings"])
        assert packet["mechanical"]["head_sha"] != "0000000"
