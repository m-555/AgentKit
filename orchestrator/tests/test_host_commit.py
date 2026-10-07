"""Host commits preserve task scope and reject stale owners before Git mutation."""
import pytest

from agentkit import db, mcp_workspaces, repo, worker, worktrees


def running(conn, project_root, project, monkeypatch):
    task_id = db.create_task(conn, title="commit", spec_id="host-commit", status="RUNNING",
                             generation=1, owned_paths=["services/retry.py"])
    task = db.get_task(conn, task_id)
    work, _ = worktrees.ensure(project_root, task, project)
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=repo.head_commit(work))
    for key, value in {"AGENTKIT_ROOT": str(project_root), "AGENTKIT_TASK": str(task_id),
                       "AGENTKIT_GENERATION": "1", "AGENTKIT_ROLE": "worker"}.items():
        monkeypatch.setenv(key, value)
    return task_id, work


def test_host_commit_keeps_only_owned_changes(conn, project_root, project, monkeypatch):
    task_id, work = running(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    result = mcp_workspaces.task_commit("Implement retry")
    assert "Committed " in result
    assert repo.head_commit(work) != before
    assert db.get_task(conn, task_id)["last_commit"] == repo.head_commit(work)
    assert db.get_task(conn, task_id)["status"] == "RUNNING"
    assert repo.changed_files(work) == []


def test_host_commit_refuses_foreign_file_before_staging(conn, project_root, project, monkeypatch):
    _, work = running(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    (work / "services/media.py").write_text("VALUE = 'foreign'\n")
    with pytest.raises(PermissionError):
        mcp_workspaces.task_commit("Invalid scope")
    assert repo.head_commit(work) == before
    assert repo.staged_files(work) == []


def test_host_commit_refuses_superseded_generation(conn, project_root, project, monkeypatch):
    task_id, work = running(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    db.update_task(conn, task_id, generation=2)
    (work / "services/retry.py").write_text("VALUE = 'zombie'\n")
    with pytest.raises(worker.StaleGeneration):
        mcp_workspaces.task_commit("Stale commit")
    assert repo.head_commit(work) == before



def test_host_commit_accepts_bounded_multiline_message(conn, project_root, project, monkeypatch):
    _, work = running(conn, project_root, project, monkeypatch)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    assert "Committed " in mcp_workspaces.task_commit("Implement retry\n\n" + "Detailed bounded evidence. " * 20)


def test_host_commit_normalizes_only_changed_audited_source(conn, project_root, project, monkeypatch):
    import yaml
    _, work = running(conn, project_root, project, monkeypatch)
    config = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["source_line_endings"] = "crlf"
    config.write_text(yaml.safe_dump(raw))
    foreign = (work / "services/media.py").read_bytes()
    (work / "services/retry.py").write_bytes(b"VALUE = 'new'\n")
    assert "Committed " in mcp_workspaces.task_commit("Implement retry")
    assert (work / "services/retry.py").read_bytes() == b"VALUE = 'new'\r\n"
    assert (work / "services/media.py").read_bytes() == foreign


def test_host_commit_refuses_oversized_message_before_staging(conn, project_root, project, monkeypatch):
    _, work = running(conn, project_root, project, monkeypatch)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    with pytest.raises(ValueError, match="1-2000"):
        mcp_workspaces.task_commit("x" * 2001)
    assert repo.staged_files(work) == []



def test_owned_tracked_file_beneath_ignored_parent_commits(conn, project_root, project, monkeypatch):
    import subprocess
    ignore = project_root / '.gitignore'
    ignore.write_text(ignore.read_text() + '\nservices/\n')
    subprocess.run(['git', 'add', '--', '.gitignore'], cwd=project_root, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'Ignore generated directory'], cwd=project_root, check=True, capture_output=True)
    _, work = running(conn, project_root, project, monkeypatch)
    (work / 'services/retry.py').write_text("VALUE = 'preserved tracked input'\n")
    assert 'Committed ' in mcp_workspaces.task_commit('Update exact tracked input')
    assert repo.is_clean(work)


def test_failed_host_static_gate_never_stages(conn, project_root, project, monkeypatch):
    from agentkit import gates
    _, work = running(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    monkeypatch.setattr(gates, "run_gate", lambda *a, **kw: gates.GateResult("fast", False, skipped_reason="broken"))
    with pytest.raises(ValueError, match="broken"):
        mcp_workspaces.task_commit("Must not commit")
    assert repo.head_commit(work) == before and not repo.staged_files(work)


def test_foreign_edits_from_gate_cannot_enter_commit(conn, project_root, project, monkeypatch):
    from agentkit import gates
    _, work = running(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    (work / "services/retry.py").write_text("VALUE = 'new'\n")
    def run(*args, **kwargs):
        (work / "services/media.py").write_text("foreign change\n")
        return gates.GateResult("fast", True)
    monkeypatch.setattr(gates, "run_gate", run)
    with pytest.raises(PermissionError):
        mcp_workspaces.task_commit("Must not include foreign files")
    assert repo.head_commit(work) == before and not repo.staged_files(work)
