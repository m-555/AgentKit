"""A failed launch preflight must not demand a checkpoint from a nonexistent worker."""
from types import SimpleNamespace

import pytest

from agentkit import db, handoff, repo, worktrees
from tests.conftest import commit_all


def reservation(conn, project_root, project):
    task_id = db.create_task(conn, title="preflight", status="READY", spec_id="preflight",
                             owned_paths=["services/retry.py"], expected_write=["services/retry.py"])
    work, _ = worktrees.ensure(project_root, db.get_task(conn, task_id), project)
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=repo.head_commit(work))
    run = db.open_worker_run(conn, task_id, 1, "claude-code", str(work))
    db.close_worker_run(conn, run, exit_code=None)
    return task_id, work, run


def check(conn, project, task_id, work):
    target = SimpleNamespace(provider="claude-code", model="claude-opus-5-5", effort="high")
    return handoff.verify(conn, project, db.get_task(conn, task_id), work, target)


def test_pristine_never_spawned_reservation_can_retry(conn, project_root, project):
    task_id, work, _ = reservation(conn, project_root, project)
    result = check(conn, project, task_id, work)
    assert result.ok, result.reason
    assert result.evidence["never_spawned"]


@pytest.mark.parametrize("change", ["dirty", "commit", "open_run", "prior_pid", "prior_session"])
def test_reservation_does_not_bypass_worker_or_byte_evidence(conn, project_root, project, change):
    task_id, work, run = reservation(conn, project_root, project)
    if change in ("dirty", "commit"):
        (work / "services/retry.py").write_text("VALUE = 'unaudited'\n")
        if change == "commit":
            commit_all(work, "unexpected source commit")
    elif change == "open_run":
        conn.execute("UPDATE worker_runs SET ended_at=NULL WHERE id=?", (run,))
    elif change == "prior_pid":
        conn.execute("UPDATE worker_runs SET pid=2147483647 WHERE id=?", (run,))
    else:
        db.update_task(conn, task_id, session_token="prior-session")
    conn.commit()
    assert not check(conn, project, task_id, work).ok
