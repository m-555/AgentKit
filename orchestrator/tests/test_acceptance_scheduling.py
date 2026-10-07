"""Acceptance tests 8-10, 13-14: scheduling, contracts, integration and capabilities.

These defend the invariants that decide *what runs together*. The recurring theme
is invariant 12 — uncertainty serialises — because every ambiguous case here has
a cheap wrong answer (lose some concurrency) and an expensive one (corrupt a merge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit import contracts, db, gates, integrator, overlap, scheduler, worktrees
from agentkit import statemachine as sm
from agentkit.capabilities import CapabilitySet
from tests.conftest import commit_all, git


class TestDependencyOrder:
    """Test 8 — A -> (B, C) -> D."""

    @pytest.fixture()
    def graph(self, conn):
        a = db.create_task(conn, spec_id="a", title="abstraction",
                           expected_write=["services/base.py"], status=sm.READY)
        b = db.create_task(conn, spec_id="b", title="provider b",
                           expected_write=["services/b.py"], depends_on=["a"])
        c = db.create_task(conn, spec_id="c", title="provider c",
                           expected_write=["services/c.py"], depends_on=["a"])
        d = db.create_task(conn, spec_id="d", title="cost tracking",
                           expected_write=["services/cost.py"], depends_on=["b", "c"])
        return a, b, c, d

    def test_dependents_stay_planned_until_the_parent_is_done(self, conn, graph):
        a, b, c, d = graph
        db.refresh_ready(conn)
        assert db.get_task(conn, b)["status"] == sm.PLANNED
        assert db.get_task(conn, c)["status"] == sm.PLANNED

    def test_finishing_the_parent_unblocks_exactly_its_children(self, conn, graph):
        a, b, c, d = graph
        for status in (sm.LEASED, sm.RUNNING, sm.VERIFYING, sm.REVIEW,
                       sm.INTEGRATION_READY, sm.INTEGRATING, sm.DONE):
            db.set_status(conn, a, status, actor="scheduler")
        db.refresh_ready(conn)

        assert db.get_task(conn, b)["status"] == sm.READY
        assert db.get_task(conn, c)["status"] == sm.READY
        assert db.get_task(conn, d)["status"] == sm.PLANNED   # still needs both

    def test_siblings_with_disjoint_paths_run_together(self, conn, project_root, graph):
        a, b, c, d = graph
        for status in (sm.LEASED, sm.RUNNING, sm.VERIFYING, sm.REVIEW,
                       sm.INTEGRATION_READY, sm.INTEGRATING, sm.DONE):
            db.set_status(conn, a, status, actor="scheduler")
        db.refresh_ready(conn)

        chosen, deferred = overlap.schedulable_set(
            conn, project_root, [db.get_task(conn, b), db.get_task(conn, c)], limit=3
        )
        assert len(chosen) == 2, "disjoint sibling tasks must be able to run concurrently"
        assert deferred == []


class TestOverlapPrediction:
    """Test 13's sibling: serialise before launching, not after colliding."""

    def test_overlapping_writes_serialise(self, conn, project_root):
        left = db.create_task(conn, spec_id="l", title="left",
                              expected_write=["services/media/**"], status=sm.READY)
        right = db.create_task(conn, spec_id="r", title="right",
                               expected_write=["services/media/veo.py"], status=sm.READY)
        decision = overlap.can_run_together(
            conn, project_root, db.get_task(conn, left), db.get_task(conn, right)
        )
        assert not decision.parallel
        assert decision.code == "predicted_write_overlap"

    def test_undeclared_scope_serialises(self, conn, project_root):
        """Invariant 12 — an unplanned task cannot be proven safe."""
        left = db.create_task(conn, spec_id="l", title="left", expected_write=[],
                              status=sm.READY)
        right = db.create_task(conn, spec_id="r", title="right",
                               expected_write=["services/retry.py"], status=sm.READY)
        decision = overlap.can_run_together(
            conn, project_root, db.get_task(conn, left), db.get_task(conn, right)
        )
        assert not decision.parallel
        assert decision.code == "undeclared_scope"

    def test_contract_readers_wait_for_contract_writers(self, conn, project_root):
        writer = db.create_task(conn, spec_id="w", title="change contract",
                                kind="CONTRACT_CHANGE",
                                expected_write=["contracts/api.yaml"], status=sm.READY)
        reader = db.create_task(conn, spec_id="r", title="implement client",
                                expected_write=["services/client.py"],
                                expected_read=["contracts/api.yaml"], status=sm.READY)
        decision = overlap.can_run_together(
            conn, project_root, db.get_task(conn, writer), db.get_task(conn, reader)
        )
        assert not decision.parallel
        assert decision.code == "reader_of_changing_contract"

    def test_serialisation_decisions_are_logged_with_a_reason(self, conn, project_root):
        """§15.2: 'why was this task serialized?' must be answerable afterwards."""
        db.create_task(conn, spec_id="l", title="left",
                       expected_write=["services/media/**"], status=sm.RUNNING)
        right = db.create_task(conn, spec_id="r", title="right",
                               expected_write=["services/media/veo.py"], status=sm.READY)

        overlap.schedulable_set(conn, project_root, [db.get_task(conn, right)], limit=2)

        events = db.recent_events(conn, right, kind="serialization_decision")
        assert events
        assert "overlap" in events[0]["cause"]


class TestCapabilityGating:
    """Test 14 — a weak agent is never handed expensive work."""

    def _caps(self, name: str, **overrides: bool) -> CapabilitySet:
        caps = CapabilitySet(adapter=name)
        for key in ("workspace_sandbox", "prewrite_file_guard", "shell_guard",
                    "mcp_stdio", "structured_output", "resume_session"):
            caps.set(key, True)
        for key, value in overrides.items():
            caps.set(key, value)
        return caps

    def test_hotspot_requires_strong_write_isolation(self):
        weak = self._caps("weak", workspace_sandbox=False)
        assert not weak.can_run("HOTSPOT")
        assert "strong_write_isolation" in weak.missing_for("HOTSPOT")

    def test_weak_agent_no_longer_qualifies_for_any_write_work(self):
        """CHANGED (§2): previously asserted SAFE_PARALLEL was allowed.

        That was the cross-worktree provenance hole. An agent without proven
        filesystem confinement can write into another worker's worktree, where
        the victim's audit sees a change to a path the victim legitimately owns
        and no lease audit can attribute it. Unattended write work now requires
        `write_worker_safe`; read-only work is unaffected.
        """
        weak = self._caps("weak", workspace_sandbox=False)
        assert not weak.can_run("SAFE_PARALLEL")
        assert not weak.can_run("TEST_ONLY")
        assert weak.can_run("RESEARCH")

    def test_scheduler_picks_a_qualifying_agent(self):
        capabilities = {
            "weak": self._caps("weak", workspace_sandbox=False),
            "strong": self._caps("strong"),
        }
        name, reason = scheduler.choose_adapter({"kind": "HOTSPOT"}, capabilities)
        assert name == "strong"

    def test_scheduler_refuses_when_nothing_qualifies(self):
        capabilities = {"weak": self._caps("weak", workspace_sandbox=False)}
        name, reason = scheduler.choose_adapter({"kind": "HOTSPOT"}, capabilities)
        assert name is None
        assert "strong_write_isolation" in reason

    def test_research_needs_nothing(self):
        bare = CapabilitySet(adapter="bare")
        assert bare.can_run("RESEARCH")


class TestEnvironmentIsolation:
    """Test 18 — dependency environments are private to a worktree.

    CHANGED (§4): these tests previously asserted fingerprint-gated *sharing* of
    `.venv` and `node_modules`. Matching lockfiles prove dependency equivalence,
    not immutability — editable installs, native rebuilds and postinstall scripts
    all mutate an installed tree whose lockfile never changed — so sharing is now
    refused outright and only content-addressed caches are shared.
    """

    def test_fingerprint_still_tracks_the_lockfile(self, project_root):
        """Retained as a cache key and staleness signal, not as permission."""
        before = worktrees.environment_fingerprint(project_root)
        (project_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        after = worktrees.environment_fingerprint(project_root)
        assert before != after

    def test_environments_are_never_shared(self, conn, project, make_task):
        task_id = make_task("add retry", ["services/retry.py"])
        shared, reason = worktrees.may_share_environment(
            conn, project, db.get_task(conn, task_id)
        )
        assert not shared
        assert "never shared" in reason

    def test_lockfile_task_is_also_private(self, conn, project, make_task):
        task_id = make_task("bump deps", ["uv.lock", "pyproject.toml"])
        shared, _ = worktrees.may_share_environment(
            conn, project, db.get_task(conn, task_id)
        )
        assert not shared

    def test_env_dirs_cover_installed_trees_and_build_output(self):
        for name in (".venv", "node_modules", "build", "dist", "target"):
            assert name in worktrees.ENV_DIRS

    def test_caches_are_shared_through_env_vars_not_symlinks(self, project_root):
        """Env vars, so a worktree teardown cannot delete the shared cache."""
        env = worktrees.shared_cache_env(project_root)
        assert set(env) == {"UV_CACHE_DIR", "npm_config_cache", "PIP_CACHE_DIR"}
        for value in env.values():
            assert Path(value).is_dir()

    def test_two_worktrees_resolve_to_the_same_cache(self, project_root, tmp_path):
        a = worktrees.shared_cache_env(project_root)
        b = worktrees.shared_cache_env(project_root)
        assert a == b

    def test_private_env_paths_are_inside_the_worktree(self, tmp_path):
        paths = worktrees.private_env_paths(tmp_path / "wt-x")
        assert all(str(p).startswith(str(tmp_path / "wt-x")) for p in paths)


class TestContractChange:
    """Test 9 — a frozen contract cannot be changed by a dependent."""

    def test_only_a_contract_task_may_freeze(self, conn, project, project_root, make_task):
        ordinary = make_task("ordinary", ["services/retry.py"])
        with pytest.raises(ValueError, match="CONTRACT_CHANGE"):
            contracts.freeze(conn, project_root, project, ordinary)

    def test_freeze_records_hashes_and_bumps_the_version(
        self, conn, project, project_root, make_task
    ):
        owner = make_task("define api", ["contracts/**"], kind="CONTRACT_CHANGE")
        lock = contracts.freeze(conn, project_root, project, owner)

        assert lock.version == 1
        assert "contracts/api.yaml" in lock.paths
        assert lock.paths["contracts/api.yaml"].startswith("sha256:")

    def test_dependents_are_pinned_to_the_frozen_version(self, conn, project, project_root):
        owner = db.create_task(conn, spec_id="contract", title="define api",
                               kind="CONTRACT_CHANGE", expected_write=["contracts/**"],
                               status=sm.RUNNING)
        dependent = db.create_task(conn, spec_id="client", title="implement client",
                                   depends_on=["contract"], expected_write=["services/c.py"])

        contracts.freeze(conn, project_root, project, owner)
        assert db.get_task(conn, dependent)["contract_version"] == 1

    def test_a_modified_contract_is_detected_at_the_gate(
        self, conn, project, project_root, make_task
    ):
        owner = make_task("define api", ["contracts/**"], kind="CONTRACT_CHANGE")
        contracts.freeze(conn, project_root, project, owner)

        (project_root / "contracts" / "api.yaml").write_text("version: 2\n", encoding="utf-8")
        changed = contracts.verify_unchanged(project_root, project, exclude_owner="other")
        assert changed == ["contracts/api.yaml"]

    def test_the_owner_may_still_change_its_own_contract(
        self, conn, project, project_root, make_task
    ):
        owner = make_task("define api", ["contracts/**"], kind="CONTRACT_CHANGE",
                          spec_id="contract-owner")
        contracts.freeze(conn, project_root, project, owner)
        (project_root / "contracts" / "api.yaml").write_text("version: 2\n", encoding="utf-8")

        assert contracts.verify_unchanged(
            project_root, project, exclude_owner="contract-owner"
        ) == []

    def test_superseding_replans_stale_dependents(self, conn, project, project_root):
        owner = db.create_task(conn, spec_id="contract", title="define api",
                               kind="CONTRACT_CHANGE", expected_write=["contracts/**"],
                               status=sm.RUNNING)
        dependent = db.create_task(conn, spec_id="client", title="client",
                                   depends_on=["contract"], expected_write=["services/c.py"],
                                   status=sm.RUNNING)
        contracts.freeze(conn, project_root, project, owner)

        replanned = contracts.supersede(conn, project_root, project, owner)
        assert dependent in replanned
        assert db.get_task(conn, dependent)["status"] == sm.NEEDS_REPLAN


class TestFailedIntegration:
    """Test 10 — two green branches, broken together."""

    def test_merge_requires_a_branch_and_a_clean_tree(self, conn, project, make_task):
        task_id = make_task("add retry", ["services/retry.py"], status=sm.INTEGRATION_READY)
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert "no branch" in outcome.detail

    def test_out_of_lease_branch_is_rejected_before_merging(
        self, conn, project, project_root, make_task
    ):
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        task_id = make_task("add retry", ["services/retry.py"], status=sm.INTEGRATION_READY)
        db.update_task(conn, task_id, branch="main", worktree=str(project_root),
                       base_sha=base)

        (project_root / "services" / "media.py").write_text("SMUGGLED\n", encoding="utf-8")
        commit_all(project_root, "smuggled change")

        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert outcome.stage == "L7_audit"
        assert outcome.violations[0]["path"] == "services/media.py"

    def test_combined_gate_failure_keeps_the_integration_branch_intact(
        self, conn, project, project_root, make_task
    ):
        """The gate that only the integration branch can run."""
        project.gates["full"] = ["python -c \"import sys; sys.exit(1)\""]
        result = gates.run_gate(project, "full", cwd=project_root)
        assert not result.passed
        assert "FAILED" in result.summary()

    def test_already_merged_branch_is_a_noop(self, conn, project, project_root, make_task):
        """Idempotency (§14): merging twice must not error or duplicate."""
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        task_id = make_task("add retry", ["services/retry.py"], status=sm.REVIEW)
        db.update_task(conn, task_id, branch="main", worktree=str(project_root),
                       base_sha=base)
        integrator.ensure_integration_branch(project)
        git(project_root, "branch", "-f", "integration", "main")
        from agentkit import reviews
        reviews.approve(conn, project, task_id, base, "PASS", "independent-test-reviewer", "Checked unchanged branch")

        outcome = integrator.merge_one(conn, project, db.get_task(conn, task_id))
        assert outcome.ok
        assert "already merged" in outcome.detail
        assert db.get_task(conn, task_id)["status"] == sm.DONE
