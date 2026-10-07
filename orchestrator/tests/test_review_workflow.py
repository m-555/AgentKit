"""Two review modes, exact-version human decisions and mechanical staging."""
from __future__ import annotations

import sys

import pytest

from agentkit import (
    db,
    gates,
    integrator,
    jobs,
    repo,
    review_policy,
    supervisor,
    user_acceptance,
    verification,
    worktrees,
)
from agentkit.config import load_project
from tests.conftest import commit_all


def configured(root, mode="human"):
    path = root / ".ai/project.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\nworkflow: {mode: separate-tasks, review: " + mode + "}\n")
    return load_project(root)


def graph(root, conn, mode="human"):
    project = configured(root, mode)
    jobs.create(root, "feature", "Improve retry while preserving media", acceptance=["Retry handles its boundary"])
    conn.execute("UPDATE jobs SET status='ACTIVE',planned_revision=1 WHERE id='feature'")
    first = db.create_task(conn, title="Build retry", spec_id="build", role="backend-builder", job_id="feature",
                           status="REVIEW", gate_level="source", owned_paths=["services/retry.py"], expected_write=["services/retry.py"])
    second = db.create_task(conn, title="Test retry", spec_id="test", role="backend-tester", job_id="feature",
                            status="PLANNED", kind="TEST_ONLY", gate_level="tests", depends_on=["build"],
                            expected_read=["services/retry.py"], owned_paths=["tests/test_retry.py"], expected_write=["tests/test_retry.py"])
    project.gates["source"] = project.gate("fast")
    project.gates["tests"] = project.gate("fast")
    # Persist the declared gates so HTTP/CLI reload sees the same authority.
    import yaml
    data = project.raw
    data["gates"]["source"] = project.gates["source"]
    data["gates"]["tests"] = project.gates["tests"]
    (root / ".ai/project.yaml").write_text(yaml.safe_dump(data))
    project = load_project(root)
    return project, first, second


def implement(root, conn, project, task_id, path, content):
    task = db.get_task(conn, task_id)
    work, _ = worktrees.ensure(root, task, project)
    base = repo.head_commit(work)
    (work / path).write_text(content)
    commit_all(work, "Fixture implementation")
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    db.update_task(conn, task_id, status="REVIEW")
    return work


def complete(root, conn, monkeypatch):
    project, first, second = graph(root, conn)
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    def no_models(*args, **kwargs):
        pytest.fail("human staging/acceptance must not start AI controls")
    monkeypatch.setattr(supervisor, "_control_launch", no_models)
    implement(root, conn, project, first, "services/retry.py", "VALUE = 'bounded'\n")
    supervisor.tick(root)
    assert db.get_task(conn, first)["status"] == "DONE"
    assert db.get_task(conn, second)["status"] == "READY"
    implement(root, conn, project, second, "tests/test_retry.py", "def test_boundary():\n    assert True\n")
    supervisor.tick(root)
    packet = user_acceptance.preview(conn, project, "feature")
    assert packet["ready"] and packet["status"] == "AWAITING_USER", packet
    return project, packet


def decide(root, packet, verdict="PASS", evidence="Tested the retry preview and its requirements"):
    return user_acceptance.decide(root, packet["job_id"], packet["revision"], packet["head"],
                                  packet["digest"], verdict, evidence)


def test_human_mode_finishes_preview_without_ai_then_requires_user(conn, project_root, monkeypatch):
    original = repo.head_commit(project_root)
    project, packet = complete(project_root, conn, monkeypatch)
    assert repo.head_commit(project_root) == original
    assert all(row[0] == review_policy.STAGING_REVIEWER for row in conn.execute("SELECT reviewer FROM reviews"))
    with pytest.raises(PermissionError, match="operator acceptance"):
        supervisor.accept_job(project_root, "feature", 1, "AI claims done")
    supervisor.tick(project_root)  # Waiting costs no model turns.
    result = decide(project_root, packet)
    assert result["status"] == "DONE"
    assert conn.execute("SELECT completed_sha FROM jobs WHERE id='feature'").fetchone()[0] == packet["head"]
    assert repo.head_commit(project_root) == original
    assert conn.execute("SELECT COUNT(*) FROM human_acceptance").fetchone()[0] == 1
    with pytest.raises(ValueError):
        decide(project_root, packet)  # Duplicate decisions do not create duplicate approvals.


def test_human_feedback_is_durable_and_requires_new_plan(conn, project_root, monkeypatch):
    _, packet = complete(project_root, conn, monkeypatch)
    result = decide(project_root, packet, "CHANGES", "The timeout message needs a clearer label")
    assert result["status"] == "PLANNING"
    job = jobs.load(project_root, "feature")
    assert job["revision"] == 2 and job["requests"][-1]["text"].startswith("The timeout")
    state = conn.execute("SELECT * FROM jobs WHERE id='feature'").fetchone()
    assert state["revision"] == 2 and state["planned_revision"] == 1
    with pytest.raises(ValueError, match="preview changed"):
        decide(project_root, packet)


def test_new_integration_commit_invalidates_human_decision(conn, project_root, monkeypatch):
    project, packet = complete(project_root, conn, monkeypatch)
    work = integrator.integration_worktree(project)
    (work / "services/media.py").write_text("VALUE = 'changed after UI test'\n")
    commit_all(work, "Fixture newer preview")
    with pytest.raises(ValueError, match="preview changed"):
        decide(project_root, packet)
    assert conn.execute("SELECT COUNT(*) FROM human_acceptance").fetchone()[0] == 0


def test_worker_cannot_impersonate_user(conn, project_root, monkeypatch):
    _, packet = complete(project_root, conn, monkeypatch)
    monkeypatch.setenv("AGENTKIT_PROCESS", "999")
    with pytest.raises(PermissionError, match="impersonate"):
        decide(project_root, packet)


def test_human_mode_requires_independent_tester(conn, project_root):
    project, first, second = graph(project_root, conn)
    db.update_task(conn, second, status="CANCELLED")
    work = implement(project_root, conn, project, first, "services/retry.py", "VALUE = 'untested'\n")
    with pytest.raises(ValueError, match="independent tester"):
        review_policy.stage(conn, project, db.get_task(conn, first))
    assert repo.is_clean(work)
    assert db.get_task(conn, first)["status"] == "REVIEW"


def test_mechanical_approval_cannot_satisfy_ai_policy(conn, project_root):
    project, first, _ = graph(project_root, conn)
    implement(project_root, conn, project, first, "services/retry.py", "VALUE = 'staged'\n")
    review_policy.stage(conn, project, db.get_task(conn, first))
    project.raw["workflow"]["review"] = "ai"
    outcome = integrator.merge_one(conn, project, db.get_task(conn, first))
    assert not outcome.ok and "AI review" in outcome.detail


def test_failed_combined_checks_never_prepare_user_preview(conn, project_root):
    project, first, _ = graph(project_root, conn)
    implement(project_root, conn, project, first, "services/retry.py", "VALUE = 'broken'\n")
    review_policy.stage(conn, project, db.get_task(conn, first))
    project.gates["full"] = [f'"{sys.executable}" -c "raise SystemExit(1)"']
    outcome = integrator.merge_one(conn, project, db.get_task(conn, first))
    assert not outcome.ok
    assert db.get_task(conn, first)["status"] == "FAILED"
    assert not user_acceptance.preview(conn, project, "feature")["ready"]


def test_staging_refuses_out_of_scope_changes(conn, project_root):
    project, first, _ = graph(project_root, conn)
    work = implement(project_root, conn, project, first, "services/retry.py", "VALUE = 'updated'\n")
    (work / "services/media.py").write_text("VALUE = 'foreign edit'\n")
    commit_all(work, "Fixture foreign change")
    with pytest.raises(ValueError, match="out-of-lease"):
        review_policy.stage(conn, project, db.get_task(conn, first))


def test_passing_checks_reused_only_for_same_clean_checkout_and_commands(conn, project, project_root, monkeypatch):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    calls = []
    original = gates.run_gate
    monkeypatch.setattr(gates, "run_gate", lambda *args, **kwargs: (calls.append(kwargs), original(*args, **kwargs))[1])
    assert verification.run(conn, project, project_root, "full").passed
    assert verification.run(conn, project, project_root, "full").passed
    assert len(calls) == 1
    project.gates["full"] = [f'"{sys.executable}" -c "print(2)"']
    assert verification.run(conn, project, project_root, "full").passed
    assert len(calls) == 2
    (project_root / "services/media.py").write_text("DIRTY = True\n")
    assert not verification.run(conn, project, project_root, "full").passed
    assert len(calls) == 2


def test_cache_invalidates_when_private_dependency_manifest_changes(conn, project, project_root):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    record = project_root / ".venv/Lib/site-packages/demo.dist-info/RECORD"
    record.parent.mkdir(parents=True)
    record.write_text("package_v1.py")
    before = verification.signature(project, project_root, "full")
    record.write_text("package_v2_with_different_dependencies.py")
    assert verification.signature(project, project_root, "full") != before


def test_saved_human_policy_cannot_be_changed_into_ai_acceptance(conn, project_root, monkeypatch):
    _, packet = complete(project_root, conn, monkeypatch)
    path = project_root / ".ai/project.yaml"
    path.write_text(path.read_text().replace("review: human", "review: ai"))
    with pytest.raises(PermissionError, match="saved human-review"):
        supervisor.accept_job(project_root, "feature", 1, "Attempted policy downgrade")
    assert conn.execute("SELECT status FROM jobs WHERE id='feature'").fetchone()[0] == "AWAITING_USER"


def test_user_can_report_followup_after_acceptance(conn, project_root, monkeypatch):
    project, packet = complete(project_root, conn, monkeypatch)
    decide(project_root, packet)
    accepted = user_acceptance.preview(conn, project, "feature")
    assert accepted["status"] == "DONE"
    decide(project_root, accepted, "CHANGES", "A follow-up case needs a clearer error")
    assert jobs.load(project_root, "feature")["revision"] == 2
