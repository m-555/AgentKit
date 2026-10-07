"""Worktrees must resolve runtime state to the main checkout.

Regression test for a bug that failed *silently*, which is the worst kind here:
`tasks.db` is gitignored, so a worker running in a linked worktree that resolved
state to its own directory created a second, empty database, saw no leases, and
happily committed another agent's files. Every enforcement layer reported clean
because each was asking a database that knew about nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentkit import audit, db
from agentkit.config import load_project
from agentkit.paths import find_project_root, main_worktree_root
from tests.conftest import commit_all, git


@pytest.fixture()
def worktree(project_root: Path, tmp_path: Path) -> Path:
    commit_all(project_root, "onboard")
    target = tmp_path / "wt-feature"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "agent/feature", str(target)],
        cwd=str(project_root), capture_output=True, timeout=120,
    )
    return target


class TestRootResolution:
    def test_main_worktree_root_from_inside_a_linked_worktree(self, project_root, worktree):
        assert main_worktree_root(worktree) == project_root.resolve()

    def test_find_project_root_resolves_to_the_main_checkout(self, project_root, worktree):
        assert find_project_root(worktree) == project_root.resolve()

    def test_partial_ai_dir_in_a_worktree_does_not_capture_the_root(
        self, project_root, worktree
    ):
        """The exact shape of the original bug.

        `install_pre_commit` creates `.ai/githooks` inside the worktree. A root
        search that stopped at any `.ai/` directory would stop there — at a
        directory with no project.yaml and no database.
        """
        (worktree / ".ai" / "githooks").mkdir(parents=True, exist_ok=True)
        assert find_project_root(worktree) == project_root.resolve()

    def test_state_is_shared_not_duplicated(self, project_root, worktree):
        conn = db.connect(find_project_root(worktree))
        try:
            task_id = db.create_task(conn, title="from the worktree",
                                     owned_paths=["services/retry.py"], status="RUNNING")
        finally:
            conn.close()

        main = db.connect(project_root)
        try:
            assert db.get_task(main, task_id) is not None
        finally:
            main.close()
        assert not (worktree / ".ai" / "tasks.db").exists(), \
            "a worktree must never create its own database"


class TestEnforcementInsideAWorktree:
    def test_lease_is_visible_from_the_worktree(self, project_root, worktree):
        root = find_project_root(worktree)
        conn = db.connect(root)
        try:
            owner = db.create_task(conn, title="owns base",
                                   owned_paths=["services/media.py"], status="RUNNING")
            intruder = db.create_task(conn, title="owns retry",
                                      owned_paths=["services/retry.py"], status="RUNNING")

            (worktree / "services" / "media.py").write_text("SMUGGLED\n", encoding="utf-8")
            git(worktree, "add", "services/media.py")

            result = audit.audit_staged(conn, load_project(root), worktree, intruder)
            assert not result.clean
            assert result.violations[0].owner_task == owner
        finally:
            conn.close()

    def test_in_scope_change_in_a_worktree_is_clean(self, project_root, worktree):
        root = find_project_root(worktree)
        conn = db.connect(root)
        try:
            task_id = db.create_task(conn, title="owns retry",
                                     owned_paths=["services/retry.py"], status="RUNNING")
            (worktree / "services" / "retry.py").write_text("legitimate\n", encoding="utf-8")
            git(worktree, "add", "services/retry.py")

            result = audit.audit_staged(conn, load_project(root), worktree, task_id)
            assert result.clean
        finally:
            conn.close()
