"""Acceptance tests 19 and 22: secret handling, and base_sha/history integrity.

Both are about evidence. §5 keeps credentials out of the places AgentKit
persists; §8 makes sure the range the merge gate audits still describes the work
it claims to — a branch that was reset behind its base, or squashed to remove the
offending commit, would otherwise present a clean diff precisely because the
evidence is gone.

Every §8 decision is made by git, never by a model.
"""

from __future__ import annotations

import pytest

from agentkit import db, integrator, repo, secrets
from agentkit import statemachine as sm
from tests.conftest import commit_all, git

SYNTHETIC = {
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "github": "ghp_0123456789abcdefghijklmnopqrstuvwx",
    "openai": "sk-abcdefghijklmnopqrstuvwxyz012345",
    "anthropic": "sk-ant-abcdefghijklmnopqrstuvwxyz0123",
    "slack": "xoxb-1111111111-aaaaaaaaaaaaaaaaaaaa",
    "google": "AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz01234567",
}


class TestRedaction:
    """Test 19 — nothing credential-shaped reaches persisted state."""

    @pytest.mark.parametrize("value", SYNTHETIC.values())
    def test_known_key_formats_are_stripped(self, value):
        assert value not in secrets.redact_text(f"token is {value} ok")

    def test_assignments_are_stripped(self):
        text = "DATABASE_PASSWORD=hunter2hunter2\nAPI_KEY: abcdef1234567890"
        cleaned = secrets.redact_text(text)
        assert "hunter2hunter2" not in cleaned
        assert "abcdef1234567890" not in cleaned

    def test_url_credentials_are_stripped(self):
        cleaned = secrets.redact_text("postgres://user:s3cretpw@db.example.com/app")
        assert "s3cretpw" not in cleaned
        assert cleaned.startswith("postgres://")

    def test_private_key_blocks_are_stripped(self):
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----"
        )
        assert "MIIEowIBAAKCAQEA" not in secrets.redact_text(block)

    def test_secret_named_dict_keys_are_stripped(self):
        cleaned = secrets.redact({"AWS_SECRET_ACCESS_KEY": "abcdefgh12345678", "ok": "fine"})
        assert cleaned["AWS_SECRET_ACCESS_KEY"] == secrets.REDACTED
        assert cleaned["ok"] == "fine"

    def test_nested_structures_are_covered(self):
        payload = {"steps": [{"cmd": f"export TOKEN={SYNTHETIC['github']}"}]}
        assert SYNTHETIC["github"] not in str(secrets.redact(payload))

    def test_ordinary_text_survives(self):
        text = "services/media.py belongs to task 3 (Extract the provider interface)"
        assert secrets.redact_text(text) == text


class TestPersistedStateIsClean:
    def test_event_log_redacts(self, conn, make_task):
        task_id = make_task("x", ["services/retry.py"])
        db.log_event(conn, task_id, "gate_run",
                     cause=f"ran with OPENAI_API_KEY={SYNTHETIC['openai']}",
                     detail={"env": {"GITHUB_TOKEN": SYNTHETIC["github"]}})
        dumped = str(db.recent_events(conn, task_id, limit=5))
        assert SYNTHETIC["openai"] not in dumped
        assert SYNTHETIC["github"] not in dumped

    def test_checkpoints_redact(self, conn, make_task):
        task_id = make_task("x", ["services/retry.py"])
        db.write_checkpoint(conn, task_id, {
            "decisions": [f"used key {SYNTHETIC['aws']}"],
            "env": {"DB_PASSWORD": "hunter2hunter2"},
        })
        saved = str(db.latest_checkpoint(conn, task_id))
        assert SYNTHETIC["aws"] not in saved
        assert "hunter2hunter2" not in saved

    def test_gate_output_redacts(self, conn, make_task):
        task_id = make_task("x", ["services/retry.py"])
        db.record_gate(conn, task_id, "fast", "abc123", False,
                       "connection failed: postgres://u:s3cretpw@host/db")
        row = db.cached_gate(conn, task_id, "fast", "abc123")
        assert "s3cretpw" not in str(row)

    def test_violation_records_redact(self, conn, make_task):
        task_id = make_task("x", ["services/retry.py"])
        db.record_violation(conn, task_id, "L5", "x.py",
                            reason=f"token {SYNTHETIC['slack']}")
        assert SYNTHETIC["slack"] not in str(db.list_violations(conn, task_id))


class TestWorkerEnvironment:
    def test_credentials_are_not_inherited(self):
        base = {
            "PATH": "/usr/bin", "HOME": "/home/x",
            "AWS_SECRET_ACCESS_KEY": SYNTHETIC["aws"],
            "GITHUB_TOKEN": SYNTHETIC["github"],
            "DATABASE_URL": "postgres://u:p@prod/db",
            "NPM_TOKEN": "abc",
        }
        env = secrets.worker_environment(base=base)
        assert env["PATH"] == "/usr/bin"
        for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DATABASE_URL", "NPM_TOKEN"):
            assert leaked not in env

    def test_allowlist_is_positive_not_a_denylist(self):
        """An unknown variable is dropped, so a new credential name is safe by default."""
        env = secrets.worker_environment(base={"SOME_FUTURE_CREDENTIAL": "x"})
        assert env == {}

    def test_agentkit_variables_are_passed_through(self):
        env = secrets.worker_environment({"AGENTKIT_TASK": "7", "AGENTKIT_GENERATION": "2"})
        assert env["AGENTKIT_TASK"] == "7"


class TestWorktreeSecretScan:
    def test_env_file_is_reported(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
        assert ".env" in secrets.scan_worktree(tmp_path)["files"]

    def test_private_key_is_reported(self, tmp_path):
        (tmp_path / "deploy.pem").write_text("x\n", encoding="utf-8")
        assert "deploy.pem" in secrets.scan_worktree(tmp_path)["files"]

    def test_symlink_escaping_the_worktree_is_reported(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "credentials").write_text("SECRET\n", encoding="utf-8")
        work = tmp_path / "wt"
        work.mkdir()
        link = work / "creds"
        try:
            link.symlink_to(outside / "credentials")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")
        assert "creds" in secrets.scan_worktree(work)["escaping_links"]

    def test_clean_worktree_reports_nothing(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        found = secrets.scan_worktree(tmp_path)
        assert found["files"] == [] and found["escaping_links"] == []

    def test_assert_clean_explains_what_to_do(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
        problems = secrets.assert_clean(tmp_path)
        assert problems and "gitignore" in problems[0]


class TestBaseShaInvariants:
    """Test 22 — the audited range must still describe this task's work."""

    @pytest.fixture()
    def merge_ready(self, conn, project_root, make_task):
        commit_all(project_root, "onboard")
        # An extra commit so that base~1 is still a fully onboarded tree; resetting
        # past the onboarding commit would remove .gitignore and trip the unclean
        # worktree check before the ancestry check under test.
        (project_root / "README.md").write_text("demo\n", encoding="utf-8")
        commit_all(project_root, "marker")
        base = git(project_root, "rev-parse", "HEAD").stdout.strip()
        task_id = make_task("add retry", ["services/retry.py"],
                            status=sm.INTEGRATION_READY)
        git(project_root, "checkout", "-q", "-b", "agent/retry")
        (project_root / "services" / "retry.py").write_text("work\n", encoding="utf-8")
        commit_all(project_root, "retry work")
        db.update_task(conn, task_id, branch="agent/retry", worktree=str(project_root),
                       base_sha=base)
        return task_id, base

    def test_healthy_branch_passes(self, conn, project, merge_ready):
        task_id, _base = merge_ready
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert outcome.ok, outcome.detail

    def test_force_reset_behind_base_is_refused(self, conn, project, project_root, merge_ready):
        task_id, base = merge_ready
        git(project_root, "reset", "--hard", f"{base}~1")
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert outcome.stage == "base_sha_invariant"
        assert "ancestor" in outcome.detail

    def test_missing_base_commit_is_refused(self, conn, project, merge_ready):
        task_id, _base = merge_ready
        db.update_task(conn, task_id, base_sha="0" * 40)
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert "gone from the repository" in outcome.detail

    def test_branch_pointing_at_another_task_is_refused(self, conn, project, merge_ready):
        task_id, _base = merge_ready
        other = db.create_task(conn, spec_id="other", title="other task",
                               status=sm.RUNNING, branch="agent/retry")
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert f"task {other}" in outcome.detail

    def test_worktree_on_the_wrong_branch_is_refused(
        self, conn, project, project_root, merge_ready
    ):
        task_id, _base = merge_ready
        git(project_root, "checkout", "-q", "-b", "agent/somewhere-else")
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert "somewhere-else" in outcome.detail

    def test_vanished_branch_is_refused(self, conn, project, project_root, merge_ready):
        task_id, _base = merge_ready
        db.update_task(conn, task_id, branch="agent/never-existed")
        outcome = integrator.verify(conn, project, db.get_task(conn, task_id))
        assert not outcome.ok
        assert "no longer exists" in outcome.detail

    def test_refusals_are_recorded(self, conn, project, project_root, merge_ready):
        task_id, base = merge_ready
        git(project_root, "reset", "--hard", f"{base}~1")
        integrator.verify(conn, project, db.get_task(conn, task_id))
        kinds = [e["kind"] for e in db.recent_events(conn, task_id, limit=20)]
        assert "integration_refused" in kinds

    def test_commit_exists_detects_rewritten_history(self, project_root):
        commit_all(project_root, "onboard")
        head = git(project_root, "rev-parse", "HEAD").stdout.strip()
        assert repo.commit_exists(project_root, head)
        assert not repo.commit_exists(project_root, "0" * 40)
