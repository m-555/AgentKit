"""Acceptance test 16: the cross-worktree provenance hole.

The scenario lease auditing cannot solve:

    Worker A owns services/a/**   in worktree wt-a
    Worker B owns services/b/**   in worktree wt-b

    A writes ../wt-b/services/b/file.py

B's audit sees a change to a path B **legitimately owns**, so it reports clean.
A's own branch never contains the change, so A's audit and A's merge gate report
clean too. No amount of after-the-fact diffing can attribute that write.

"L7 catches it later" is therefore *not* an acceptable answer here, and these
tests deliberately do not accept it. The fix has to be isolation: an agent that
cannot prove filesystem confinement is never given unattended write work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentkit import audit, db, scheduler
from agentkit.capabilities import READONLY_TASK_KINDS, WRITE_TASK_KINDS, CapabilitySet
from agentkit.config import load_project
from agentkit.paths import canonical_relpath, find_project_root
from tests.conftest import commit_all


def _caps(name: str, **overrides: bool) -> CapabilitySet:
    caps = CapabilitySet(adapter=name)
    for key in ("workspace_sandbox", "prewrite_file_guard", "shell_guard",
                "mcp_stdio", "structured_output", "resume_session", "event_stream"):
        caps.set(key, True)
    for key, value in overrides.items():
        caps.set(key, value)
    return caps


class TestWriteWorkerSafety:
    """No proven confinement, no unattended write work."""

    @pytest.mark.parametrize("kind", WRITE_TASK_KINDS)
    def test_write_kinds_require_confinement(self, kind):
        unconfined = _caps("unconfined", workspace_sandbox=False)
        assert not unconfined.can_run(kind), f"{kind} must require write_worker_safe"
        assert "write_worker_safe" in unconfined.missing_for(kind)

    @pytest.mark.parametrize("kind", READONLY_TASK_KINDS)
    def test_readonly_kinds_do_not(self, kind):
        unconfined = _caps("unconfined", workspace_sandbox=False)
        assert unconfined.can_run(kind), f"{kind} should not need confinement"

    def test_confined_agent_qualifies(self):
        assert _caps("confined").can_run("SAFE_PARALLEL")

    def test_scheduler_refuses_write_work_to_an_unconfined_agent(self):
        capabilities = {"unconfined": _caps("unconfined", workspace_sandbox=False)}
        name, reason = scheduler.choose_adapter({"kind": "SAFE_PARALLEL"}, capabilities)
        assert name is None
        assert "write_worker_safe" in reason

    def test_scheduler_still_gives_it_research(self):
        capabilities = {"unconfined": _caps("unconfined", workspace_sandbox=False)}
        name, _ = scheduler.choose_adapter({"kind": "RESEARCH"}, capabilities)
        assert name == "unconfined"

    def test_scheduler_prefers_a_confined_agent_for_writes(self):
        capabilities = {
            "unconfined": _caps("unconfined", workspace_sandbox=False),
            "confined": _caps("confined"),
        }
        name, _ = scheduler.choose_adapter({"kind": "HOTSPOT"}, capabilities)
        assert name == "confined"

    def test_confinement_is_not_vendor_specific(self):
        """Any mechanism that sets the capability qualifies — no vendor names."""
        for vendor in ("some-future-agent", "aider", "opencode"):
            assert _caps(vendor).can_run("SAFE_PARALLEL")


class TestCrossWorktreeWrite:
    """A real second worktree, and a real attempt to reach into it."""

    @pytest.fixture()
    def two_worktrees(self, project_root: Path, tmp_path: Path):
        commit_all(project_root, "onboard")
        (project_root / "services" / "a").mkdir(parents=True, exist_ok=True)
        (project_root / "services" / "b").mkdir(parents=True, exist_ok=True)
        (project_root / "services" / "a" / "file.py").write_text("A\n", encoding="utf-8")
        (project_root / "services" / "b" / "file.py").write_text("B\n", encoding="utf-8")
        commit_all(project_root, "two services")

        wt_a = tmp_path / "wt-a"
        wt_b = tmp_path / "wt-b"
        for path, branch in ((wt_a, "agent/a"), (wt_b, "agent/b")):
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", branch, str(path)],
                cwd=str(project_root), capture_output=True, timeout=120,
            )
        return project_root, wt_a, wt_b

    def test_absolute_path_into_another_worktree_is_refused(self, two_worktrees):
        """Canonicalisation refuses it before any lease logic runs."""
        _root, wt_a, wt_b = two_worktrees
        victim = wt_b / "services" / "b" / "file.py"
        assert canonical_relpath(str(victim), wt_a) is None

    def test_relative_traversal_into_another_worktree_is_refused(self, two_worktrees):
        _root, wt_a, wt_b = two_worktrees
        assert canonical_relpath("../wt-b/services/b/file.py", wt_a) is None

    def test_the_hole_is_real_without_isolation(self, two_worktrees):
        """Demonstrates *why* §2 needs a capability, not more auditing.

        This is the negative control: with the write already performed, neither
        worker's audit can attribute it. If this test ever starts failing because
        an audit caught it, the capability requirement could be reconsidered.
        """
        root, _wt_a, wt_b = two_worktrees
        conn = db.connect(find_project_root(root))
        project = load_project(find_project_root(root))
        try:
            task_b = db.create_task(
                conn, spec_id="b", title="owns b",
                owned_paths=["services/b/**"], status="RUNNING",
            )
            # A reaches into B's worktree by any means; the bytes are simply there.
            (wt_b / "services" / "b" / "file.py").write_text("WRITTEN BY A\n", encoding="utf-8")

            victim_audit = audit.audit_worktree(conn, project, wt_b, task_b, record=False)
            assert victim_audit.clean, (
                "B's audit reports clean because B owns that path — which is exactly "
                "why provenance cannot be recovered after the fact"
            )
        finally:
            conn.close()

    def test_worktrees_are_genuinely_separate_checkouts(self, two_worktrees):
        _root, wt_a, wt_b = two_worktrees
        (wt_a / "services" / "a" / "file.py").write_text("changed in A\n", encoding="utf-8")
        assert (wt_b / "services" / "a" / "file.py").read_text(encoding="utf-8") == "A\n"

    def test_both_worktrees_share_one_state_database(self, two_worktrees):
        root, wt_a, wt_b = two_worktrees
        assert find_project_root(wt_a) == find_project_root(wt_b) == root.resolve()
        assert not (wt_a / ".ai" / "tasks.db").exists()
        assert not (wt_b / ".ai" / "tasks.db").exists()
