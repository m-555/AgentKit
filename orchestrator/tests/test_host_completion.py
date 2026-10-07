"""Host completion observes real Git, checkpoint drift and gate failure."""
import pytest

from agentkit import checkpoints, db, gates, host_completion, repo, statemachine, worktrees


def preserved(conn, project_root, project, monkeypatch):
    identifier = db.create_task(conn, title="preserved", spec_id="preserved", status="FAILED",
                                generation=1, owned_paths=["services/retry.py"], gate_level="fast")
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=repo.head_commit(work))
    (work / "services/retry.py").write_text("VALUE = 'preserved'\n")
    checkpoints.write_mechanical(conn, project_root, work, identifier, "worker_exit")
    monkeypatch.setattr(host_completion, "authority", lambda *args: "test-native-manager")
    return identifier, work


def test_finishes_preserved_bytes_without_a_worker_launch(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    hook = project_root / ".git/hooks/pre-commit"
    hook.write_text(f'#!/bin/sh\n[ "$AGENTKIT_ROLE" = "host-completion" ] && [ "$AGENTKIT_TASK" = "{identifier}" ]\n')
    hook.chmod(0o755)
    head = host_completion.complete(conn, project, identifier, "Finish preserved worker edit")
    assert repo.is_clean(work) and repo.head_commit(work) == head
    assert db.get_task(conn, identifier)["status"] == "REVIEW"
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    assert (work / "services/retry.py").read_text() == "VALUE = 'preserved'\n"


@pytest.mark.parametrize("drift", ["bytes", "foreign", "generation", "branch"])
def test_drift_or_foreign_edits_never_stage_or_commit(conn, project_root, project, monkeypatch, drift):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    before = repo.head_commit(work)
    if drift == "bytes":
        (work / "services/retry.py").write_text("later edit\n")
    elif drift == "foreign":
        (work / "services/media.py").write_text("foreign\n")
        checkpoints.write_mechanical(conn, project_root, work, identifier, "inspected")
    elif drift == "generation":
        db.bump_generation(conn, identifier)
    else:
        db.update_task(conn, identifier, branch="wrong")
    with pytest.raises((ValueError, PermissionError)):
        host_completion.complete(conn, project, identifier, "Unsafe")
    assert repo.head_commit(work) == before and not repo.staged_files(work)


def test_gate_failure_preserves_edits_and_failed_status(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    monkeypatch.setattr(gates, "run_gate", lambda *args, **kwargs: gates.GateResult("fast", False, skipped_reason="failed"))
    before = repo.head_commit(work)
    with pytest.raises(ValueError):
        host_completion.complete(conn, project, identifier, "Blocked")
    assert repo.head_commit(work) == before and repo.changed_files(work)
    assert db.get_task(conn, identifier)["status"] == "FAILED"


def test_worker_cannot_use_host_recovery_transition():
    with pytest.raises(statemachine.TransitionError):
        statemachine.validate("FAILED", "VERIFYING", "agent")


def test_worker_cannot_impersonate_host_manager(conn, project, monkeypatch):
    monkeypatch.setenv("AGENTKIT_TASK", "5")
    with pytest.raises(PermissionError, match="worker"):
        host_completion.authority(conn, project, {"id": 5})


def test_stopped_budget_can_finish_without_another_ai_session(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    db.update_task(conn, identifier, status="BLOCKED", blocker="[AgentKit execution limit] max_tool_calls reached")
    head = host_completion.complete(conn, project, identifier, "Finish stopped bounded task")
    assert db.get_task(conn, identifier)["status"] == "REVIEW"
    assert repo.head_commit(work) == head and repo.is_clean(work)
    assert not statemachine.can("BLOCKED", "VERIFYING", "agent")
    assert not statemachine.can("BLOCKED", "VERIFYING", "hook")


def test_other_blocks_cannot_be_finished_as_budget_stops(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    db.update_task(conn, identifier, status="BLOCKED", blocker="user paused")
    before = repo.head_commit(work)
    with pytest.raises(ValueError):
        host_completion.complete(conn, project, identifier, "Must stay blocked")
    assert repo.head_commit(work) == before


def committed_preserved(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    repo._git(["add", "services/retry.py"], work, strict=True)
    repo._git(["commit", "-qm", "worker candidate"], work, strict=True)
    head = repo.head_commit(work)
    db.update_task(conn, identifier, last_commit=head)
    checkpoints.write_mechanical(conn, project_root, work, identifier, "worker_exit")
    return identifier, work, head


def test_rechecks_clean_failed_candidate_without_empty_commit(conn, project_root, project, monkeypatch):
    identifier, work, head = committed_preserved(conn, project_root, project, monkeypatch)
    assert host_completion.complete(conn, project, identifier, "Recheck preserved candidate") == head
    assert repo.head_commit(work) == head and repo.is_clean(work)
    assert db.get_task(conn, identifier)["status"] == "REVIEW"
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0


@pytest.mark.parametrize("drift", ["missing_record", "wrong_record", "empty_candidate"])
def test_clean_recheck_requires_exact_nonempty_recorded_commit(conn, project_root, project, monkeypatch, drift):
    identifier, work, head = committed_preserved(conn, project_root, project, monkeypatch)
    if drift == "missing_record":
        db.update_task(conn, identifier, last_commit=None)
    elif drift == "wrong_record":
        db.update_task(conn, identifier, last_commit=db.get_task(conn, identifier)["base_sha"])
    else:
        db.update_task(conn, identifier, base_sha=head)
    with pytest.raises(ValueError):
        host_completion.complete(conn, project, identifier, "Refuse unproven candidate")
    assert repo.head_commit(work) == head and db.get_task(conn, identifier)["status"] == "FAILED"


def test_failed_clean_recheck_keeps_commit_and_never_approves(conn, project_root, project, monkeypatch):
    identifier, work, head = committed_preserved(conn, project_root, project, monkeypatch)
    from agentkit import verification
    monkeypatch.setattr(verification, "run", lambda *args: gates.GateResult("fast", False, skipped_reason="failed"))
    with pytest.raises(ValueError):
        host_completion.complete(conn, project, identifier, "Gate still fails")
    assert repo.head_commit(work) == head and repo.is_clean(work)
    assert db.get_task(conn, identifier)["status"] == "FAILED"
    assert not conn.execute("SELECT * FROM reviews WHERE task_id=?", (identifier,)).fetchone()


def test_stopped_stale_work_can_finish_without_relaunch(conn, project_root, project, monkeypatch):
    identifier, work = preserved(conn, project_root, project, monkeypatch)
    conn.execute("UPDATE tasks SET status='STALE' WHERE id=?", (identifier,))
    head = host_completion.complete(conn, project, identifier, 'finish exact stale worker bytes')
    assert head == repo.head_commit(work)
    assert db.get_task(conn, identifier)['status'] == 'REVIEW'
    assert conn.execute('SELECT count(*) FROM processes').fetchone()[0] == 0
    with pytest.raises(statemachine.TransitionError):
        statemachine.validate('STALE', 'VERIFYING', 'agent')
