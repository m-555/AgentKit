"""A late usage report preserves completed handoff for explicit manager review."""
from types import SimpleNamespace

import pytest

from agentkit import db, repo, reviews, runner, verification


@pytest.mark.parametrize("state", ["REVIEW", "STALE", "FAILED", "INTEGRATING"])
def test_late_limit_preserves_existing_handoff_state(project_root, conn, state):
    task = db.create_task(conn, title="Completed handoff", generation=1,
                          status=state, worktree=str(project_root),
                          owned_paths=["services/retry.py"])
    runner.finish_worker(conn, project_root,
                         {"task_id": task, "generation": 1, "provider": "claude-code"},
                         0, "[AgentKit execution limit] max_output_tokens reached")
    row = db.get_task(conn, task)
    assert row["status"] == state
    assert row["blocker"].startswith("[AgentKit execution limit]")
    assert not conn.execute("SELECT 1 FROM reviews").fetchone()
    assert not conn.execute("SELECT 1 FROM provider_state").fetchone()


def test_explicit_passing_review_resolves_recorded_blocker(project_root, conn, project, monkeypatch):
    head = repo.head_commit(project_root)
    task = db.create_task(conn, title="Completed handoff", generation=1,
                          status="REVIEW", worktree=str(project_root),
                          owned_paths=["services/retry.py"], base_sha=head,
                          blocker="[AgentKit execution limit] max_output_tokens reached")
    db.update_task(conn, task, blocker="[AgentKit execution limit] max_output_tokens reached")
    assert db.get_task(conn, task)["blocker"]
    monkeypatch.setattr(verification, "run", lambda *a, **kw:
                        SimpleNamespace(passed=True, summary=lambda: "assigned gate passed"))
    reviews.approve(conn, project, task, head, "PASS", "independent-manager",
                    "Inspected preserved clean exact commit and passing assigned gate")
    row = db.get_task(conn, task)
    assert row["status"] == "INTEGRATION_READY"
    assert row["blocker"] is None
    assert db.cached_gate(conn, task, row["gate_level"], head)["passed"]
