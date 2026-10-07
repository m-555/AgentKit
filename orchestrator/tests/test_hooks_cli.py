"""The hook exit-code contract.

Claude Code reads exit codes, not return values: 0 allows, 2 blocks and shows
stderr to the model. Getting this wrong silently disables enforcement, so it is
worth testing directly rather than through the CLI.
"""

import io
import json
import subprocess
import sys

import pytest

from agentkit import db, hooks_cli
from agentkit.init_project import init


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "media.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "services" / "retry.py").write_text("x = 1\n", encoding="utf-8")
    # Checkpoints collect actual Git state; this fixture must be a real repository.
    for command in (
        ["init", "-q"],
        ["config", "user.name", "AgentKit hook test"],
        ["config", "user.email", "hooks@agentkit.test"],
        ["config", "commit.gpgsign", "false"],
        ["add", "services"],
        ["commit", "-qm", "hook fixture"],
    ):
        subprocess.run(["git", *command], cwd=tmp_path, check=True, capture_output=True)
    init(tmp_path)
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    return tmp_path


def _payload(repo, rel, tool="Edit"):
    return {
        "tool_name": tool,
        "tool_input": {"file_path": str(repo / rel)},
        "cwd": str(repo),
        "session_id": "test-session",
    }


def _run(event, payload, monkeypatch, task=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    if task is None:
        monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    else:
        monkeypatch.setenv("AGENTKIT_TASK", str(task))
    return hooks_cli.main([event])


class TestPreToolUse:
    def test_allows_owned_path(self, repo, monkeypatch):
        conn = db.connect(repo)
        task = db.create_task(conn, title="retry", owned_paths=["services/retry.py"],
                              status="RUNNING")
        conn.close()
        assert _run("pre-tool-use", _payload(repo, "services/retry.py"), monkeypatch, task) == 0

    def test_blocks_foreign_path_with_exit_2(self, repo, monkeypatch, capsys):
        conn = db.connect(repo)
        db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING")
        intruder = db.create_task(conn, title="retry", owned_paths=["services/retry.py"],
                                  status="RUNNING")
        conn.close()
        code = _run("pre-tool-use", _payload(repo, "services/media.py"), monkeypatch, intruder)
        assert code == 2
        assert "blocked" in capsys.readouterr().err.lower()

    def test_ignores_non_write_tools(self, repo, monkeypatch):
        conn = db.connect(repo)
        task = db.create_task(conn, title="x", owned_paths=["services/retry.py"], status="RUNNING")
        conn.close()
        payload = _payload(repo, "services/media.py", tool="Read")
        assert _run("pre-tool-use", payload, monkeypatch, task) == 0

    def test_taskless_session_cannot_write_a_leased_path(self, repo, monkeypatch):
        """CHANGED (§3): was `test_no_task_means_no_enforcement`.

        The old test asserted that a session with no task id could edit a file a
        running worker held — which was the bypass, not a feature. This repo is
        managed (the fixture runs `init`), so the write is now refused and the
        operator is told how to claim the path explicitly.
        """
        conn = db.connect(repo)
        db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING")
        conn.close()
        assert _run("pre-tool-use", _payload(repo, "services/media.py"), monkeypatch) == 2

    def test_taskless_session_is_told_how_to_proceed(self, repo, monkeypatch, capsys):
        conn = db.connect(repo)
        db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING")
        conn.close()
        _run("pre-tool-use", _payload(repo, "services/media.py"), monkeypatch)
        assert "agentkit operator acquire" in capsys.readouterr().err

    def test_multiedit_payload_shape(self, repo, monkeypatch):
        conn = db.connect(repo)
        db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING")
        intruder = db.create_task(conn, title="retry", owned_paths=["services/retry.py"],
                                  status="RUNNING")
        conn.close()
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {"edits": [{"file_path": str(repo / "services" / "media.py")}]},
            "cwd": str(repo),
        }
        assert _run("pre-tool-use", payload, monkeypatch, intruder) == 2


class TestFailOpen:
    """A bug in the hook must never wedge a session."""

    def test_malformed_json_does_not_block(self, repo, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
        monkeypatch.setenv("AGENTKIT_TASK", "1")
        assert hooks_cli.main(["pre-tool-use"]) == 0

    def test_unknown_event_is_ignored(self, repo, monkeypatch):
        assert _run("no-such-event", {}, monkeypatch) == 0

    def test_internal_error_allows(self, repo, monkeypatch, capsys):
        def boom(_payload):
            raise RuntimeError("simulated failure")

        monkeypatch.setitem(hooks_cli.HANDLERS, "pre-tool-use", boom)
        code = _run("pre-tool-use", _payload(repo, "services/media.py"), monkeypatch, 1)
        assert code == 0
        assert "error" in capsys.readouterr().err.lower()


class TestCheckpointHooks:
    def test_pre_compact_writes_a_checkpoint(self, repo, monkeypatch):
        conn = db.connect(repo)
        task = db.create_task(conn, title="x", owned_paths=["services/retry.py"],
                              status="RUNNING")
        conn.close()
        assert _run("pre-compact", {"cwd": str(repo)}, monkeypatch, task) == 0
        conn = db.connect(repo)
        saved = db.latest_checkpoint(conn, task)
        conn.close()
        assert saved is not None
        assert saved["reason"] == "pre_compact"

    def test_session_start_emits_the_brief(self, repo, monkeypatch, capsys):
        conn = db.connect(repo)
        task = db.create_task(conn, title="Split media", owned_paths=["services/media.py"],
                              status="RUNNING")
        conn.close()
        assert _run("session-start", {"cwd": str(repo)}, monkeypatch, task) == 0
        assert "Split media" in capsys.readouterr().out
