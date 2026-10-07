"""CLI attachment and MCP verdicts enforce the configured review authority."""
from __future__ import annotations

import os

import pytest

from agentkit import cli, db, jobs, manager, mcp_server, repo, worktrees
from agentkit.config import load_project
from tests.conftest import commit_all


@pytest.fixture
def review_task(project_root, conn, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_PROCESS", raising=False)
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    config = project_root / ".ai" / "project.yaml"
    with config.open("a", encoding="utf-8") as stream:
        stream.write("review_mode: external-manager\n")
    commit_all(project_root, "configure native manager reviews")
    jobs.create(project_root, "native-review", "Implement retry behavior", "codex")
    task_id = db.create_task(conn, title="worker result", status="REVIEW",
                            owned_paths=["services/retry.py"], expected_write=["services/retry.py"])
    db.update_task(conn, task_id, job_id="native-review")
    work, _ = worktrees.ensure(project_root, _task(conn, task_id), load_project(project_root))
    base = repo.head_commit(work)
    (work / "services" / "retry.py").write_text("VALUE = 'reviewed'\n", encoding="utf-8")
    head = commit_all(work, "implement assigned retry behavior")
    db.update_task(conn, task_id, worktree=str(work), base_sha=base,
                   branch=repo.current_branch(work), session_token="worker-session")
    return task_id, work, head


def _task(conn, task_id) -> dict:
    result = db.get_task(conn, task_id)
    assert result is not None
    return result


def _attach(project_root):
    return cli.main(["--path", str(project_root), "manager", "attach", "native-review",
                     "--holder", "root-reviewer", "--pid", str(os.getpid())])


def _normal_mode(project_root):
    path = project_root / ".ai" / "project.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("review_mode: external-manager", "review_mode: supervised"),
                    encoding="utf-8")


def _control(conn, task_id, head, purpose, monkeypatch):
    row = conn.execute(
        "INSERT INTO processes(purpose,provider,task_id,job_id,status,session_token,"
        "expected_head,launch_json,started_at) VALUES(?, 'codex', ?, 'native-review',"
        "'RUNNING', 'fresh-control-session', ?, '{}', ?)",
        (purpose, task_id if purpose == "review" else None, head, db.utcnow()),
    )
    monkeypatch.setenv("AGENTKIT_PROCESS", str(row.lastrowid))


def test_cli_attach_then_review_uses_native_identity_and_exact_clean_commit(
    project_root, conn, review_task, monkeypatch, tmp_path, capsys,
):
    task_id, work, head = review_task
    monkeypatch.setenv("CODEX_THREAD_ID", "native-manager-session")
    assert _attach(project_root) == 0
    lease = manager.lease(conn, "native-review")
    assert lease is not None
    assert lease["session_ref"] == "native-manager-session"
    credential = project_root / ".ai" / "runtime" / "manager-native-review.credential"
    assert credential.read_text().strip() not in capsys.readouterr().out
    evidence = tmp_path / "review evidence.txt"
    evidence.write_text("Read the exact retry diff; verified the assigned value and gates.", encoding="utf-8")
    assert cli.main(["--path", str(project_root), "manager", "review", "native-review",
                     "--task", str(task_id), "--head", head, "--verdict", "PASS",
                     "--evidence-file", str(evidence)]) == 0
    saved = conn.execute("SELECT * FROM reviews WHERE task_id=?", (task_id,)).fetchone()
    assert saved["head_sha"] == head and saved["reviewer"] == "external-manager:root-reviewer"
    assert _task(conn, task_id)["status"] == "INTEGRATION_READY"
    gate = db.cached_gate(conn, task_id, "fast", head)
    assert gate is not None and gate["passed"]
    assert repo.head_commit(work) == head and repo.is_clean(work)


def test_external_cli_attach_without_identity_fails_before_claiming_lease(
    project_root, conn, review_task, capsys,
):
    assert _attach(project_root) == 2
    assert "CODEX_THREAD_ID" in capsys.readouterr().err
    assert manager.lease(conn, "native-review") is None
    assert not (project_root / ".ai" / "runtime" / "manager-native-review.credential").exists()


def test_normal_cli_attach_can_omit_native_identity(project_root, conn, review_task):
    _normal_mode(project_root)
    assert _attach(project_root) == 0
    lease = manager.lease(conn, "native-review")
    assert lease is not None and lease["session_ref"] == ""


@pytest.mark.parametrize("purpose", ["review", "coordinator"])
def test_external_mode_rejects_supervised_mcp_verdicts(
    project_root, conn, review_task, monkeypatch, purpose,
):
    task_id, _, head = review_task
    _control(conn, task_id, head, purpose, monkeypatch)
    with pytest.raises(PermissionError, match="external-manager"):
        mcp_server.review_submit(task_id, head, "PASS", "Read the complete diff")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert _task(conn, task_id)["status"] == "REVIEW"


@pytest.mark.parametrize("purpose", ["review", "coordinator"])
def test_normal_mode_preserves_supervised_mcp_reviews(
    project_root, conn, review_task, monkeypatch, purpose,
):
    task_id, _, head = review_task
    _normal_mode(project_root)
    _control(conn, task_id, head, purpose, monkeypatch)
    assert mcp_server.review_submit(task_id, head, "PASS", "Read exact worker diff and gates") == "Review recorded."
    assert _task(conn, task_id)["status"] == "INTEGRATION_READY"
    assert conn.execute("SELECT head_sha FROM reviews WHERE task_id=?", (task_id,)).fetchone()[0] == head


def test_external_native_mcp_review_uses_cli_attachment(project_root, conn, review_task, monkeypatch):
    task_id, _, head = review_task
    monkeypatch.setenv("CODEX_THREAD_ID", "native-manager-session")
    assert _attach(project_root) == 0
    assert mcp_server.review_submit(task_id, head, "PASS", "Read exact retry diff and passing gates") == "External manager review recorded."
    assert _task(conn, task_id)["status"] == "INTEGRATION_READY"
