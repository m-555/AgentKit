"""Acceptance tests 17 and 20: unmanaged-session denial, and zombie generations.

Two different ways a write can arrive from something that is not a current,
scoped worker:

* a human (or a manually opened agent session) with no task at all — §3;
* a worker from a superseded generation still running after recovery — §6.

Both must be refused, and both must leave a trace explaining why.
"""

from __future__ import annotations

import pytest

from agentkit import db, operator, worker
from agentkit.config import ProjectConfig
from agentkit.leases import decide


class TestUnmanagedSessionDenial:
    """Test 17 — a session with no task may not ignore active leases."""

    def test_managed_repo_denies_a_taskless_write(self, conn, project, make_task):
        make_task("owns media", ["services/media.py"])
        verdict = decide(conn, project, "services/media.py", None)
        assert not verdict.allowed
        assert verdict.code == "unmanaged_session_blocked"

    def test_managed_repo_denies_even_an_unclaimed_path(self, conn, project):
        """Default-deny, not deny-if-contested.

        Allowing unclaimed paths would leave a race: a scheduler could grant that
        exact path to a worker a millisecond later.
        """
        verdict = decide(conn, project, "services/nobody_owns_this.py", None)
        assert not verdict.allowed
        assert verdict.code == "unmanaged_session"

    def test_denial_names_the_escape_hatch(self, conn, project):
        verdict = decide(conn, project, "services/x.py", None)
        assert "agentkit operator acquire" in verdict.reason

    def test_unmanaged_repository_is_untouched(self, tmp_path, conn):
        """A repo that never ran `init` must behave exactly as before."""
        bare = ProjectConfig(root=tmp_path / "not-onboarded", name="bare")
        verdict = decide(conn, bare, "anything.py", None)
        assert verdict.allowed
        assert verdict.code == "unmanaged_repository"


class TestOperatorLease:
    def test_operator_can_claim_and_then_write(self, conn, project):
        result = operator.acquire(conn, ["services/retry.py"], reason="hotfix")
        assert result.ok

        verdict = decide(conn, project, "services/retry.py", None)
        assert verdict.allowed
        assert verdict.code == "operator_lease"

    def test_claim_does_not_cover_other_paths(self, conn, project):
        operator.acquire(conn, ["services/retry.py"])
        assert not decide(conn, project, "services/media.py", None).allowed

    def test_operator_refused_where_a_worker_is_active(self, conn, project, make_task):
        make_task("owns media", ["services/media.py"])
        result = operator.acquire(conn, ["services/media.py"])

        assert not result.ok
        assert result.conflicts
        assert "revoke" in result.message

    def test_worker_refused_where_the_operator_holds(self, conn, project, make_task):
        operator.acquire(conn, ["services/media.py"])
        worker_task = make_task("wants media", [])
        verdict = decide(conn, project, "services/media.py", worker_task)
        assert not verdict.allowed
        assert verdict.code == "owned_by_other"

    def test_release_frees_the_paths(self, conn, project):
        operator.acquire(conn, ["services/retry.py"])
        operator.release(conn)
        verdict = decide(conn, project, "services/retry.py", None)
        assert not verdict.allowed

    def test_operator_actions_are_in_the_event_log(self, conn, project):
        operator.acquire(conn, ["services/retry.py"], reason="hotfix")
        kinds = [e["kind"] for e in db.recent_events(conn, limit=50)]
        assert "operator_lease_acquired" in kinds

        operator.release(conn)
        kinds = [e["kind"] for e in db.recent_events(conn, limit=50)]
        assert "operator_lease_released" in kinds

    def test_refusal_is_also_logged(self, conn, project, make_task):
        make_task("owns media", ["services/media.py"])
        operator.acquire(conn, ["services/media.py"])
        kinds = [e["kind"] for e in db.recent_events(conn, limit=50)]
        assert "operator_lease_refused" in kinds

    def test_claim_is_visible_in_status(self, conn, project):
        from agentkit import observability

        operator.acquire(conn, ["services/retry.py"])
        rendered = observability.render_status(conn)
        assert "OPERATOR" in rendered


class TestZombieGeneration:
    """Test 20 — generation N cannot mutate after N+1 exists."""

    @pytest.fixture()
    def recovered(self, conn, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        db.bump_generation(conn, task_id)          # -> 1, the live worker
        db.bump_generation(conn, task_id)          # -> 2, after recovery
        return task_id

    def test_current_generation_is_accepted(self, conn, recovered):
        ctx = worker.require_current(conn, recovered, worker_generation=2)
        assert ctx.is_current

    def test_stale_generation_is_refused(self, conn, recovered):
        with pytest.raises(worker.StaleGeneration):
            worker.require_current(conn, recovered, worker_generation=1)

    def test_refusal_is_recorded(self, conn, recovered):
        with pytest.raises(worker.StaleGeneration):
            worker.require_current(conn, recovered, worker_generation=1)
        kinds = [e["kind"] for e in db.recent_events(conn, task_id=recovered, limit=20)]
        assert "stale_generation" in kinds

    def test_refusal_explains_what_to_do(self, conn, recovered):
        try:
            worker.require_current(conn, recovered, worker_generation=0)
        except worker.StaleGeneration as exc:
            assert "Stop work and exit" in str(exc)
        else:
            raise AssertionError("expected StaleGeneration")

    def test_state_is_unchanged_after_a_stale_attempt(self, conn, recovered):
        before = db.get_task(conn, recovered)
        with pytest.raises(worker.StaleGeneration):
            worker.require_current(conn, recovered, worker_generation=1)
        after = db.get_task(conn, recovered)
        assert after["status"] == before["status"]
        assert after["generation"] == before["generation"]

    def test_missing_generation_is_treated_as_a_human_caller(self, conn, recovered):
        """The CLI and tests have no AGENTKIT_GENERATION; they are not zombies."""
        ctx = worker.require_current(conn, recovered, worker_generation=None)
        assert ctx.is_current

    def test_guard_does_not_raise_for_hooks(self, conn, recovered):
        ctx = worker.guard(conn, recovered)
        assert ctx is not None
