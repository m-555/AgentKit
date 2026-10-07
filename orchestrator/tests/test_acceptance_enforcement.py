"""Acceptance tests 1-3: the enforcement guarantees.

These decide whether PLAN_V3 §2.3 is true. A guarantee without a passing test
here is downgraded to a hope, so each test is named for the claim it defends.
"""

from __future__ import annotations

import pytest

from agentkit import audit, db, shellguard
from agentkit.leases import decide
from agentkit.paths import resolve_within
from tests.conftest import commit_all, git


class TestCollision:
    """Test 1 — two agents, one file. One wins, the other is blocked."""

    def test_owner_may_edit(self, conn, project, make_task):
        task = make_task("split media", ["services/media.py"])
        assert decide(conn, project, "services/media.py", task).allowed

    def test_other_agent_is_blocked(self, conn, project, make_task):
        owner = make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])
        verdict = decide(conn, project, "services/media.py", intruder)
        assert not verdict.allowed
        assert verdict.owner_task == owner

    def test_block_reason_is_actionable(self, conn, project, make_task):
        """A blocked agent must know who owns it and what to do instead."""
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])
        reason = decide(conn, project, "services/media.py", intruder).reason
        assert "lease_request" in reason
        assert "split media" in reason


class TestBashBypass:
    """Test 2 — the channels a PreToolUse hook cannot see.

    v2 claimed a worker "physically cannot" edit outside its lease. It can: shell
    redirection, sed, interpreters and package managers all bypass the tool hook.
    These tests prove the claim v3 actually makes — such a change is detected by
    L5, refused by L6, and can never reach integration through L7.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "echo BREACHED > services/media.py",
            "echo BREACHED >> services/media.py",
            "sed -i 's/a/b/' services/media.py",
            "cp /tmp/x services/media.py",
            "rm services/media.py",
            "git checkout HEAD -- services/media.py",
        ],
    )
    def test_statically_decidable_writes_are_blocked(
        self, conn, project, project_root, make_task, command
    ):
        """L4 blocks what it can prove writes out of scope."""
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])

        def authorize(path: str):
            rel = resolve_within(path, project_root) or path
            return decide(conn, project, rel, intruder)

        verdict = shellguard.classify(command, authorize, [])
        assert not verdict.allowed, f"{command!r} should have been blocked"

    @pytest.mark.parametrize(
        "command",
        [
            "python -c \"open('services/media.py','w').write('BREACHED')\"",
            "bash scripts/rewrite.sh",
            "node -e \"require('fs').writeFileSync('x','y')\"",
            "npm install",
            "make build",
        ],
    )
    def test_opaque_writes_are_denied_by_default(
        self, conn, project, project_root, make_task, command
    ):
        """Undecidable write targets are denied, not hoped about."""
        intruder = make_task("add retry", ["services/retry.py"])

        def authorize(path: str):
            rel = resolve_within(path, project_root) or path
            return decide(conn, project, rel, intruder)

        verdict = shellguard.classify(command, authorize, [])
        assert not verdict.allowed
        assert verdict.opaque
        assert "cannot be determined" in verdict.reason

    def test_project_gate_commands_are_allowlisted(
        self, conn, project, project_root, make_task
    ):
        """A project's own declared commands must not be collateral damage."""
        task = make_task("add retry", ["services/retry.py"])

        def authorize(path: str):
            rel = resolve_within(path, project_root) or path
            return decide(conn, project, rel, task)

        verdict = shellguard.classify(
            "python -m pytest -q", authorize, ["python -m pytest -q"]
        )
        assert verdict.allowed

    def test_reads_are_never_blocked(self, conn, project, project_root, make_task):
        task = make_task("add retry", ["services/retry.py"])

        def authorize(path: str):
            rel = resolve_within(path, project_root) or path
            return decide(conn, project, rel, task)

        for command in ("cat services/media.py", "grep -r TODO .", "git status"):
            assert shellguard.classify(command, authorize, []).allowed, command

    def test_bypass_is_detected_by_the_worktree_audit(
        self, conn, project, project_root, make_task
    ):
        """L5: whatever the channel, git sees the change.

        Simulated the way a real bypass looks to core — the bytes are simply
        there, with no tool call to have intercepted.
        """
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])

        (project_root / "services" / "media.py").write_text("BREACHED\n", encoding="utf-8")

        result = audit.audit_worktree(conn, project, project_root, intruder)
        assert not result.clean
        assert result.violations[0].path == "services/media.py"

    def test_bypass_is_recorded_as_a_violation(self, conn, project, project_root, make_task):
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])
        (project_root / "services" / "media.py").write_text("BREACHED\n", encoding="utf-8")

        audit.audit_worktree(conn, project, project_root, intruder)
        violations = db.list_violations(conn, intruder)
        assert violations and violations[0]["layer"] == "L5"

    def test_bypass_cannot_be_committed(self, conn, project, project_root, make_task):
        """L6: staged out-of-lease changes are refused."""
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])

        (project_root / "services" / "media.py").write_text("BREACHED\n", encoding="utf-8")
        git(project_root, "add", "services/media.py")

        result = audit.audit_staged(conn, project, project_root, intruder)
        assert not result.clean
        assert result.layer == "L6"

    def test_bypass_cannot_reach_integration(self, conn, project, project_root, make_task):
        """L7: the guarantee stated without qualification.

        Even with every earlier layer bypassed and the change committed, the
        merge gate rejects it from the complete branch diff.
        """
        make_task("split media", ["services/media.py"])
        intruder = make_task("add retry", ["services/retry.py"])
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()

        (project_root / "services" / "media.py").write_text("BREACHED\n", encoding="utf-8")
        (project_root / "services" / "retry.py").write_text("legitimate\n", encoding="utf-8")
        commit_all(project_root, "in-scope change plus a smuggled one")

        result = audit.audit_branch(conn, project, project_root, intruder, base)
        assert not result.clean
        assert [v.path for v in result.violations] == ["services/media.py"]
        assert "services/retry.py" in result.checked   # the legitimate change is fine

    def test_generated_files_are_not_treated_as_violations(
        self, conn, project, project_root, make_task
    ):
        """Build output is regenerated on the integration branch, never merged."""
        task = make_task("add retry", ["services/retry.py"])
        cache = project_root / "services" / "__pycache__"
        cache.mkdir()
        (cache / "media.cpython-311.pyc").write_bytes(b"\x00")

        result = audit.audit_worktree(conn, project, project_root, task)
        assert result.clean


class TestCrossWorktreeWrite:
    """Test 3 — absolute paths into another worker's tree."""

    def test_paths_outside_the_repo_are_rejected(self, project_root):
        assert resolve_within("/etc/passwd", project_root) is None
        assert resolve_within("C:/Windows/system32/x.dll", project_root) is None

    def test_traversal_is_resolved_before_authorisation(self, project_root):
        escaped = resolve_within(str(project_root / ".." / "other" / "file.py"), project_root)
        assert escaped is None

    def test_framework_state_is_never_writable(self, conn, project, make_task):
        task = make_task("anything", [".ai/**"])
        verdict = decide(conn, project, ".ai/tasks.db", task)
        assert not verdict.allowed
        assert verdict.code == "protected_state"


class TestProtectedPaths:
    def test_hotspot_needs_explicit_ownership(self, conn, project, make_task):
        task = make_task("unscoped", [])
        verdict = decide(conn, project, "routes/video.py", task)
        assert not verdict.allowed
        assert verdict.code == "protected_path"

    def test_contract_paths_are_protected(self, conn, project, make_task):
        task = make_task("unscoped", [])
        verdict = decide(conn, project, "contracts/api.yaml", task)
        assert not verdict.allowed
        assert "contract" in verdict.reason

    def test_explicit_owner_may_edit_a_hotspot(self, conn, project, make_task):
        task = make_task("split router", ["routes/video.py"], kind="DECOUPLE")
        assert decide(conn, project, "routes/video.py", task).allowed
