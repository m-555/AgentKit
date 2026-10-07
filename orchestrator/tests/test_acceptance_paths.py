"""Acceptance tests 21 and 23: path roles, and hostile-input canonicalisation.

Authorisation must reason about the canonical filesystem target, not about how a
string looks. Anything that cannot be proven to resolve inside the tree returns
None — and every caller treats None as **refuse**, never as "unknown, allow".

The property test at the end is the important one: it asserts that no generated
path, however mangled, ever escapes while still being reported as inside.
"""

from __future__ import annotations

import os
import random
import subprocess
from typing import ClassVar

import pytest

from agentkit import db
from agentkit.paths import (
    canonical_relpath,
    escapes_root,
    find_project_root,
    is_absolute_like,
    is_symlink_escape,
    normalize,
    repo_common_root,
    state_root,
    worktree_root,
)


class TestAbsoluteDetection:
    @pytest.mark.parametrize("raw", [
        "C:\\foo", "c:/foo", "\\\\server\\share", "//server/share",
        "/etc/passwd", "/", "\\windows\\system32",
    ])
    def test_absolute_forms_are_recognised_on_every_platform(self, raw):
        assert is_absolute_like(raw), f"{raw!r} must be treated as absolute"

    @pytest.mark.parametrize("raw", [
        "services/media.py", "./a.py", "a/b/c", "file.txt", "..",
    ])
    def test_relative_forms_are_not(self, raw):
        assert not is_absolute_like(raw)


class TestEscapes:
    @pytest.mark.parametrize("raw", [
        "../escape.py",
        "a/../../escape.py",
        "a/b/../../../escape.py",
        "./../../escape.py",
        "..\\escape.py",
        "a\\..\\..\\escape.py",
        "/etc/passwd",
        "C:/Windows/System32/drivers/etc/hosts",
        "//server/share/x",
        "\\\\server\\share\\x",
    ])
    def test_escaping_paths_are_refused(self, raw, tmp_path):
        assert canonical_relpath(raw, tmp_path) is None, f"{raw!r} escaped"

    @pytest.mark.parametrize("raw", [
        "services/media.py",
        "./services/media.py",
        "services//media.py",
        "services/./media.py",
        "services/sub/../media.py",
        "services\\media.py",
        "a/b/../../services/media.py",
    ])
    def test_inside_paths_resolve_to_the_same_thing(self, raw, tmp_path):
        (tmp_path / "services").mkdir()
        assert canonical_relpath(raw, tmp_path) == "services/media.py"

    def test_the_root_itself_is_not_a_writable_path(self, tmp_path):
        assert canonical_relpath(".", tmp_path) is None
        assert canonical_relpath(str(tmp_path), tmp_path) is None

    def test_empty_and_whitespace_are_refused(self, tmp_path):
        for raw in ("", "   ", "\t"):
            assert canonical_relpath(raw, tmp_path) is None

    def test_missing_files_are_still_authorisable(self, tmp_path):
        """An agent creating a new file must not be blocked by its absence."""
        assert canonical_relpath("services/new_file.py", tmp_path) == "services/new_file.py"

    def test_missing_file_under_escaping_parent_is_refused(self, tmp_path):
        assert canonical_relpath("../elsewhere/new_file.py", tmp_path) is None


class TestSymlinks:
    @pytest.fixture()
    def linked(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "target.py").write_text("x\n", encoding="utf-8")
        inside = tmp_path / "repo"
        (inside / "services").mkdir(parents=True)
        try:
            (inside / "services" / "link.py").symlink_to(outside / "target.py")
            (inside / "linkdir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")
        return inside, outside

    def test_symlinked_file_escaping_the_root_is_refused(self, linked):
        inside, _outside = linked
        assert canonical_relpath("services/link.py", inside) is None

    def test_write_through_a_symlinked_directory_is_refused(self, linked):
        inside, _outside = linked
        assert canonical_relpath("linkdir/new.py", inside) is None

    def test_symlink_escape_is_reported_separately(self, linked):
        inside, _outside = linked
        assert is_symlink_escape("services/link.py", inside)

    def test_ordinary_files_are_not_flagged(self, linked):
        inside, _outside = linked
        (inside / "services" / "real.py").write_text("x\n", encoding="utf-8")
        assert not is_symlink_escape("services/real.py", inside)


class TestWindowsQuirks:
    @pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
    def test_trailing_dot_and_space_do_not_evade(self, tmp_path):
        """`secret.env.` and `secret.env ` open the same file on Windows."""
        for raw in ("services/media.py.", "services/media.py "):
            assert canonical_relpath(raw, tmp_path) == "services/media.py"

    @pytest.mark.skipif(os.name != "nt", reason="case-insensitive filesystem")
    def test_case_differences_resolve_inside(self, tmp_path):
        (tmp_path / "services").mkdir()
        (tmp_path / "services" / "media.py").write_text("x\n", encoding="utf-8")
        assert canonical_relpath("SERVICES/MEDIA.PY", tmp_path) is not None


class TestNormalizeIsNotAuthorisation:
    def test_normalize_collapses_separators(self):
        assert normalize("a//b\\c/") == "a/b/c"

    def test_normalize_does_not_resolve_traversal(self):
        """Deliberate: normalize is for display, canonical_relpath is for gating."""
        assert ".." in normalize("a/../../b")


class TestPathRoles:
    """Test 21 — repo-global versus worktree-local, asserted on a real worktree."""

    @pytest.fixture()
    def linked_worktree(self, project_root, tmp_path):
        from tests.conftest import commit_all

        commit_all(project_root, "onboard")
        target = tmp_path / "wt-roles"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "agent/roles", str(target)],
            cwd=str(project_root), capture_output=True, timeout=120,
        )
        return project_root, target

    def test_worktree_root_is_the_worktree(self, linked_worktree):
        _main, work = linked_worktree
        assert worktree_root(work) == work.resolve()

    def test_repo_common_root_is_the_main_checkout(self, linked_worktree):
        main, work = linked_worktree
        assert repo_common_root(work) == main.resolve()

    def test_state_root_is_repo_global(self, linked_worktree):
        main, work = linked_worktree
        assert state_root(work) == main.resolve()
        assert find_project_root(work) == main.resolve()

    def test_database_is_never_created_inside_a_worktree(self, linked_worktree):
        main, work = linked_worktree
        conn = db.connect(state_root(work))
        try:
            db.create_task(conn, title="from worktree", status="RUNNING")
        finally:
            conn.close()
        assert db.db_path(main).exists()
        assert not (work / ".ai" / "tasks.db").exists()

    def test_committed_config_is_visible_from_the_worktree(self, linked_worktree):
        _main, work = linked_worktree
        assert (work / ".ai" / "project.yaml").is_file()
        assert (work / "AGENTS.md").is_file()

    def test_runtime_state_files_are_not_in_the_worktree(self, linked_worktree):
        _main, work = linked_worktree
        for runtime in ("tasks.db", "capabilities.json"):
            assert not (work / ".ai" / runtime).exists(), (
                f"{runtime} must be repo-global, never checked into a worktree"
            )


class TestPathFuzz:
    """Test 23 — generated hostile input must never be misreported as inside."""

    SEGMENTS: ClassVar[list[str]] = [
        "a", "b", "..", ".", "", "services", "media.py", " ", "x ", "x.",
        "C:", "\\\\server", "~", "%2e%2e", "..\\", "../",
    ]

    def _cases(self, count: int, seed: int) -> list[str]:
        rng = random.Random(seed)
        cases = []
        for _ in range(count):
            depth = rng.randint(1, 5)
            sep = rng.choice(["/", "\\"])
            cases.append(sep.join(rng.choice(self.SEGMENTS) for _ in range(depth)))
        return cases

    @pytest.mark.parametrize("seed", range(6))
    def test_no_generated_path_is_ever_misreported_as_inside(self, tmp_path, seed):
        """The invariant: if it resolves inside, it really is inside."""
        root = (tmp_path / "repo").resolve()
        root.mkdir()
        checked = 0
        for raw in self._cases(400, seed):
            rel = canonical_relpath(raw, root)
            checked += 1
            if rel is None:
                continue
            assert not rel.startswith("/") and not rel.startswith("..")
            resolved = (root / rel).resolve(strict=False)
            assert str(resolved).startswith(str(root)), (
                f"{raw!r} -> {rel!r} resolved outside the root"
            )
        assert checked == 400

    @pytest.mark.parametrize("seed", range(4))
    def test_canonicalisation_is_deterministic(self, tmp_path, seed):
        root = (tmp_path / "repo").resolve()
        root.mkdir()
        for raw in self._cases(200, seed + 100):
            assert canonical_relpath(raw, root) == canonical_relpath(raw, root)

    def test_never_raises_on_hostile_input(self, tmp_path):
        root = (tmp_path / "repo").resolve()
        root.mkdir()
        hostile = [
            "\x00", "a\x00b", "\n", "a\nb", "con", "nul", "aux",
            "." * 300, "a/" * 200 + "b", "\ufeffservices/media.py",
            # Right-to-left override, and a Cyrillic lookalike letter. Written
            # as escapes so the homoglyph is visible in review, not invisible.
            "services/‮media.py", "services/mediа.py",  # noqa: RUF001
        ]
        for raw in hostile:
            canonical_relpath(raw, root)          # must not raise
            escapes_root(raw, root)

    def test_unicode_variants_do_not_produce_two_identities(self, tmp_path):
        """NFC and NFD spellings of the same name must not authorise differently."""
        root = (tmp_path / "repo").resolve()
        root.mkdir()
        nfc = "services/café.py"
        nfd = "services/cafe\u0301.py"
        a, b = canonical_relpath(nfc, root), canonical_relpath(nfd, root)
        assert (a is None) == (b is None)
