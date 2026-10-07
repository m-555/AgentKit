"""Write-tool guard for a session that has no AgentKit task.

A root or operator session is judged by the repository that contains each
target file, not by the directory the session started in. Paths outside every
managed repository have no lease to protect. A path in a managed repository
still needs that repository's operator lease. Sessions with a task keep the
strict worktree boundary.
"""

import io
import json
import subprocess
import sys

import pytest

from agentkit import db, hooks_cli, operator
from agentkit.init_project import init

_SUPERVISION_VARIABLES = ("AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS",
                          "AGENTKIT_WORKTREE", "AGENTKIT_ROOT")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _managed_repo(path):
    (path / "services").mkdir(parents=True)
    (path / "services" / "media.py").write_text("x = 1\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "AgentKit hook test")
    _git(path, "config", "user.email", "hooks@agentkit.test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "add", "services")
    _git(path, "commit", "-qm", "fixture")
    init(path)
    return path


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    for name in _SUPERVISION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    session = _managed_repo(tmp_path / "session")
    other = _managed_repo(tmp_path / "other")
    outside = tmp_path / "plain"
    outside.mkdir()
    return session, other, outside


def _edit(target, cwd, monkeypatch):
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)},
               "cwd": str(cwd), "session_id": "untasked-test"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hooks_cli.main(["pre-tool-use"])


def _operator_lease(root, path):
    conn = db.connect(root)
    try:
        assert operator.acquire(conn, [path], reason="test").granted
    finally:
        conn.close()


def test_untasked_write_outside_every_managed_repo_is_allowed(repos, monkeypatch):
    session, _other, outside = repos
    assert _edit(outside / "notes.md", session, monkeypatch) == 0


def test_untasked_write_in_the_session_repo_still_needs_an_operator_lease(repos, monkeypatch, capsys):
    session, _other, _outside = repos
    assert _edit(session / "services" / "media.py", session, monkeypatch) == 2
    assert "agentkit operator acquire" in capsys.readouterr().err
    _operator_lease(session, "services/media.py")
    assert _edit(session / "services" / "media.py", session, monkeypatch) == 0


def test_untasked_write_in_another_repo_is_judged_by_that_repos_leases(repos, monkeypatch, capsys):
    session, other, _outside = repos
    _operator_lease(session, "services/media.py")
    assert _edit(other / "services" / "media.py", session, monkeypatch) == 2
    message = capsys.readouterr().err
    assert "agentkit operator acquire" in message
    assert "Workers may only" not in message
    _operator_lease(other, "services/media.py")
    assert _edit(other / "services" / "media.py", session, monkeypatch) == 0


def test_untasked_write_into_a_linked_worktree_respects_the_owning_task(repos, monkeypatch):
    session, other, _outside = repos
    linked = other.parent / "other-worktree"
    _git(other, "worktree", "add", "-q", "-b", "agent/linked", str(linked))
    conn = db.connect(other)
    try:
        db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING",
                       worktree=str(linked))
    finally:
        conn.close()
    assert _edit(linked / "services" / "media.py", session, monkeypatch) == 2


def test_a_session_with_a_task_keeps_the_worktree_boundary(repos, monkeypatch):
    session, _other, outside = repos
    conn = db.connect(session)
    try:
        task = db.create_task(conn, title="media", owned_paths=["services/media.py"], status="RUNNING")
    finally:
        conn.close()
    monkeypatch.setenv("AGENTKIT_TASK", str(task))
    assert _edit(outside / "notes.md", session, monkeypatch) == 2
