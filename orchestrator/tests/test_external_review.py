"""External review keeps live manager identity and independent-commit checks."""
import os
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import db, external_review, jobs, manager, reviews


@pytest.fixture
def attached(project_root, project, conn, monkeypatch):
    jobs.create(project_root, "external-review", "Implement approved workflow", "codex")
    token = manager.attach(conn, project_root, "external-review", "root-reviewer", os.getpid(),
                           session_ref="native-root")
    credential = project_root / ".ai" / "runtime" / "manager-external-review.credential"
    credential.parent.mkdir(exist_ok=True)
    credential.write_text(token)
    task_id = db.create_task(conn, title="worker change", status="REVIEW")
    db.update_task(conn, task_id, job_id="external-review")
    project.raw["review_mode"] = "external-manager"
    monkeypatch.setenv("CODEX_THREAD_ID", "native-root")
    monkeypatch.delenv("AGENTKIT_PROCESS", raising=False)
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    calls = []
    monkeypatch.setattr(reviews, "approve", lambda *args: calls.append(args))
    return task_id, calls


def test_authenticated_root_uses_existing_exact_commit_approval(project, conn, attached):
    task_id, calls = attached
    external_review.submit(conn, project, task_id, "exact-head", "pass", "Read exact diff and gates")
    assert len(calls) == 1
    assert calls[0][2:5] == (task_id, "exact-head", "PASS")
    assert calls[0][5] == "external-manager:root-reviewer"


@pytest.mark.parametrize("condition", ["unconfigured", "worker", "wrong-session", "expired", "recovery", "same-worker-session"])
def test_unauthorized_or_stale_manager_cannot_review(project, conn, attached, monkeypatch, condition):
    task_id, calls = attached
    if condition == "unconfigured":
        project.raw.pop("review_mode")
    elif condition == "worker":
        monkeypatch.setenv("AGENTKIT_TASK", str(task_id))
    elif condition == "wrong-session":
        monkeypatch.setenv("CODEX_THREAD_ID", "another-chat")
    elif condition == "expired":
        expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        conn.execute("UPDATE manager_leases SET heartbeat_at=?", (expired,))
    elif condition == "recovery":
        from agentkit import manager_state
        manager_state.record_outage(conn, "external-review", "codex", "quota interruption")
    else:
        db.update_task(conn, task_id, session_token="native-root")
    with pytest.raises(PermissionError):
        external_review.submit(conn, project, task_id, "exact-head", "PASS", "Evidence")
    assert not calls
