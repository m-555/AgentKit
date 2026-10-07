"""Workflow regressions: real Git and subprocesses, simulated provider responses."""
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from agentkit import (
    db,
    gates,
    integrator,
    jobs,
    planning,
    processes,
    providers,
    quota,
    repo,
    reviews,
    spec,
    supervisor,
    worktrees,
)
from agentkit.adapters.base import Launch
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import ProjectConfig
from tests.conftest import commit_all


def caps():
    c = CapabilitySet(adapter="codex")
    for k in c.values:
        c.set(k, True)
    return c


def reviewed_change(conn, project, root, *, kind="SAFE_PARALLEL", path="services/retry.py"):
    identifier = db.create_task(conn, spec_id="change", title="change", kind=kind,
        status="REVIEW", expected_write=[path], owned_paths=[path])
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(root, task, project)
    base = repo.head_commit(work)
    (work / path).write_text("VALUE = 'updated'\n")
    commit_all(work, "implement change")
    head = repo.head_commit(work)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    reviews.approve(conn, project, identifier, head, "PASS", "independent", "Verified behavior and diff")
    return identifier, work, head


def test_undeclared_gate_fails(tmp_path):
    result = gates.run_gate(ProjectConfig(root=tmp_path), "typo")
    assert not result.passed and not result.results


@pytest.mark.parametrize("definition", [
    {"kind": "SAFE_PARALLELL"}, {"expected_paths": {"write": "src/**"}},
    {"expected_paths": {"write": ["../escape"]}}, {"skils": []},
])
def test_malformed_tasks_rejected(definition):
    with pytest.raises(ValueError):
        spec.TaskSpec.from_dict({"id": "test", "title": "Test", **definition})
    assert not CapabilitySet(adapter="empty").can_run("SAFE_PARALLELL")


def test_planned_task_survives_runtime_rebuild(project_root):
    identifier = planning.create(project_root, title="Implement retry", expected_write=["services/retry.py"])
    assert spec.load(project_root)[0].spec_id == "implement-retry"
    conn = db.connect(project_root)
    conn.execute("DELETE FROM tasks WHERE id=?", (identifier,))
    conn.close()
    from agentkit.reconcile import reconcile
    reconcile(project_root)
    conn = db.connect(project_root)
    assert db.get_task_by_spec(conn, "implement-retry") is not None
    conn.close()


def test_job_keeps_original_intent_and_provider(project_root):
    jobs.create(project_root, "draft", "Build a calendar", "codex")
    jobs.amend(project_root, "draft", "Support offline editing", user=True)
    jobs.amend(project_root, "draft", "Use the existing local store")
    job = jobs.load(project_root, "draft")
    assert job["coordinator"] == "codex"
    assert job["revision"] == 2
    assert job["requests"][0]["text"] == "Build a calendar"
    assert len(job["requests"]) == 2 and len(job["decisions"]) == 1


def test_timer_is_not_proof_of_availability(conn):
    providers.begin_cooldown(conn, "codex", reason="limit", retry_at=datetime.now(UTC) - timedelta(seconds=1))
    assert not providers.is_available(conn, "codex")
    providers.refresh(conn, checker=lambda _: {"available": None, "reason": "offline"})
    assert not providers.is_available(conn, "codex")
    providers.refresh(conn, datetime.now(UTC) + timedelta(hours=2), checker=lambda _: {"available": True})
    assert providers.is_available(conn, "codex")


def test_weekly_limit_outlasts_short_limit(conn):
    now = datetime.now(UTC)
    providers.observe(conn, "codex", {"available": False, "windows": [
        {"window": "300", "used_percent": 100, "resets_at": (now + timedelta(hours=1)).timestamp()},
        {"window": "10080", "used_percent": 100, "resets_at": (now + timedelta(days=4)).timestamp()},
    ]})
    assert db.parse_ts(providers.get_state(conn, "codex").retry_at) > now + timedelta(days=3)
    providers.observe(conn, "codex", {"available": True}, now=now + timedelta(hours=2))
    assert not providers.is_available(conn, "codex")


def test_explicit_weekday_reset_is_not_tomorrow():
    now = datetime(2026, 9, 21, 10, tzinfo=UTC)
    reset = providers.parse_retry_at("Weekly limit reached; resets Fri 3pm", now=now)
    assert reset.weekday() == 4 and reset > now + timedelta(days=3)


def test_checkout_failure_never_runs_command_on_current_branch(project_root):
    result = integrator._git(project_root, ["branch", "should-not-exist"], "missing-branch")
    assert result.returncode
    assert not repo.branch_exists(project_root, "should-not-exist")


def test_protected_integration_target_rejected(project):
    project.raw["integration_branch"] = "main"
    with pytest.raises(ValueError, match="protected"):
        integrator.ensure_integration_branch(project)


def test_later_commit_invalidates_review(conn, project, project_root):
    identifier, work, _ = reviewed_change(conn, project, project_root)
    (work / "services/retry.py").write_text("VALUE = 'unreviewed'\n")
    commit_all(work, "unreviewed edit")
    outcome = integrator.merge_one(conn, project, db.get_task(conn, identifier))
    assert not outcome.ok and "review" in outcome.detail


def test_merge_preserves_operator_checkout(conn, project, project_root):
    identifier, work, head = reviewed_change(conn, project, project_root)
    main = repo.head_commit(project_root)
    (project_root / "operator-notes.txt").write_text("Keep my unfinished notes")
    outcome = integrator.merge_one(conn, project, db.get_task(conn, identifier))
    assert outcome.ok, outcome.detail
    assert repo.head_commit(project_root) == main
    assert repo.current_branch(project_root) == "main"
    assert (project_root / "operator-notes.txt").read_text() == "Keep my unfinished notes"
    assert repo.is_ancestor(project_root, head, "integration")


def test_combined_failure_restores_exact_integration_head(conn, project, project_root):
    identifier, _, _ = reviewed_change(conn, project, project_root)
    before = repo.rev_parse(project_root, "integration")
    project.gates["full"] = [f'"{sys.executable}" -c "raise SystemExit(1)"']
    outcome = integrator.merge_one(conn, project, db.get_task(conn, identifier))
    assert not outcome.ok and outcome.stage == "combined_gate"
    assert repo.rev_parse(project_root, "integration") == before


def test_contract_freezes_before_dependents_launch(conn, project, project_root):
    identifier, _, _ = reviewed_change(conn, project, project_root, kind="CONTRACT_CHANGE", path="contracts/api.yaml")
    dependent = db.create_task(conn, spec_id="client", title="Client", depends_on=["change"])
    outcome = integrator.merge_one(conn, project, db.get_task(conn, identifier))
    assert outcome.ok, outcome.detail
    assert db.get_task(conn, dependent)["contract_version"] == 1
    assert db.get_task(conn, dependent)["status"] == "READY"
    assert (integrator.integration_worktree(project) / ".ai/contracts.lock").exists()


def test_coordinator_never_fails_over(project_root, conn, monkeypatch):
    job = jobs.create(project_root, "draft", "Build it", "codex", reviewers=["codex", "claude-code"])
    providers.begin_cooldown(conn, "codex", reason="weekly", retry_at=datetime.now(UTC) + timedelta(days=2))
    launched = []
    monkeypatch.setattr(processes, "start", lambda *a, **k: launched.append(k))
    supervisor._control_launch(conn, ProjectConfig(root=project_root), job, "coordinator")
    assert launched == []


def test_worker_handoff_preserves_base_and_attempts(project_root, conn):
    save_cache(project_root, {"codex": caps()})
    task_id = db.create_task(conn, spec_id="paused", title="Paused", status="RUNNING",
        owned_paths=["services/retry.py"], expected_write=["services/retry.py"], adapter="claude-code", base_sha="original")
    providers.begin_cooldown(conn, "claude-code", reason="weekly", retry_at=datetime.now(UTC) + timedelta(days=2))
    quota.pause(conn, task_id, provider="claude-code", retry_at=None)
    supervisor.handoff_waiting(conn, project_root)
    task = db.get_task(conn, task_id)
    assert task["status"] == "READY" and task["attempts"] == 0
    assert task["base_sha"] == "original" and task["adapter"] == "claude-code"


def test_supervised_worker_exit_reaches_review(project_root, project, conn, tmp_path):
    """Real monitor + child processes, without calling either paid provider."""
    identifier = db.create_task(conn, spec_id="worker", title="Worker", status="RUNNING",
        owned_paths=["services/retry.py"], expected_write=["services/retry.py"], generation=1)
    task = db.get_task(conn, identifier)
    work, _ = worktrees.ensure(project_root, task, project)
    db.update_task(conn, identifier, worktree=str(work), branch=repo.current_branch(work), base_sha=repo.head_commit(work))
    script = tmp_path / "fake_worker.py"
    script.write_text('''import json, subprocess
from pathlib import Path
print(json.dumps({"type":"thread.started","thread_id":"test-session"}), flush=True)
Path("services/retry.py").write_text("VALUE = 'tested'\\n")
subprocess.run(["git","add","services/retry.py"], check=True)
subprocess.run(["git","-c","commit.gpgsign=false","commit","-qm","worker change"], check=True)
print(json.dumps({"type":"turn.completed"}), flush=True)
''')
    run_id = processes.start(conn, project_root, Launch([sys.executable, str(script)], cwd=str(work)),
        purpose="worker", provider="codex", task_id=identifier, generation=1)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        process = processes.get(conn, run_id)
        if process["status"] not in ("STARTING", "RUNNING"):
            break
        time.sleep(0.2)
    assert process["status"] == "FINISHED", process
    task = db.get_task(conn, identifier)
    assert task["status"] == "REVIEW", task
    assert task["session_token"] == "test-session"
    assert db.cached_gate(conn, identifier, "fast", repo.head_commit(work))["passed"]


def test_complete_job_with_real_monitors(project_root, conn, monkeypatch, tmp_path):
    """Exercise plan -> worker -> independent reviewer -> merge -> final acceptance."""
    from agentkit import adapters, runtime_identity, scheduler
    from agentkit.adapters.base import Installation
    script = tmp_path / "fake_agent.py"
    script.write_text('''import json, os, subprocess
from pathlib import Path
from agentkit import db, mcp_server as api, repo
root = Path(os.environ["AGENTKIT_ROOT"])
role = os.environ["AGENTKIT_ROLE"]
print(json.dumps({"type":"thread.started", "thread_id":"session-" + role}), flush=True)
if role == "coordinator":
    conn = db.connect(root)
    tasks = db.list_tasks(conn)
    conn.close()
    if tasks and all(t["status"] == "DONE" for t in tasks):
        api.job_accept("draft", 1, "Read integrated retry module and verified acceptance")
    else:
        api.task_define({"id":"slice", "job_id":"draft", "title":"Runnable slice",
            "expected_paths":{"write":["services/retry.py"]},
            "acceptance":["The retry module provides the draft implementation"]})
        api.job_plan_ready("draft", 1, "One runnable slice with a focused scope")
elif role == "worker":
    Path("services/retry.py").write_text("VALUE = 'draft'\\n")
    subprocess.run(["git", "add", "services/retry.py"], check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Implement draft"], check=True)
elif role == "review":
    api.review_submit(int(os.environ["AGENTKIT_TASK"]), repo.head_commit(Path.cwd()),
        "PASS", "Read the complete diff and verified the requested value")
print(json.dumps({"type":"turn.completed"}), flush=True)
''', encoding="utf-8")
    adapter = adapters.get("codex")
    simulated_install = Installation("codex", str(script), "test", source="test-fixture")
    monkeypatch.setattr(adapter, "detect", lambda: simulated_install)
    def launch(task, work, role, project, **kwargs):
        return Launch([sys.executable, str(script)], cwd=str(work), env={
            "AGENTKIT_ROOT": str(project.root), "AGENTKIT_TASK": str(task.get("id", "")),
            "AGENTKIT_GENERATION": str(task.get("generation", 0))})
    monkeypatch.setattr(adapter, "build_launch", launch)
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *a: None)
    measured = caps()
    measured.version = simulated_install.version
    measured.installation = runtime_identity.identify(simulated_install)
    save_cache(project_root, {"codex": measured})
    jobs.create(project_root, "draft", "Create the retry draft", "codex", reviewers=["codex"])
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        conn.execute("UPDATE jobs SET next_check=NULL WHERE id='draft'")
        supervisor.tick(project_root)
        scheduler.run_once(project_root, max_workers=1)
        state = dict(conn.execute("SELECT * FROM jobs WHERE id='draft'").fetchone())
        if state["status"] == "DONE":
            break
        time.sleep(0.15)
    assert state["status"] == "DONE", (state, [dict(r) for r in conn.execute("SELECT * FROM processes")])
    assert state["coordinator_session"] == "session-coordinator"
    assert state["completed_sha"] == repo.rev_parse(project_root, "integration")
    assert repo.current_branch(project_root) == "main"
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE verdict='PASS'").fetchone()[0] == 1


def test_contract_gate_failure_rolls_back_version(conn, project, project_root):
    identifier, _, _ = reviewed_change(conn, project, project_root, kind="CONTRACT_CHANGE", path="contracts/api.yaml")
    dependent = db.create_task(conn, spec_id="consumer", title="Consumer", depends_on=["change"])
    original = repo.rev_parse(project_root, "integration")
    project.gates["full"] = [f'"{sys.executable}" -c "raise SystemExit(1)"']
    outcome = integrator.merge_one(conn, project, db.get_task(conn, identifier))
    assert not outcome.ok
    assert repo.rev_parse(project_root, "integration") == original
    assert db.get_task(conn, dependent)["contract_version"] is None
    assert not (integrator.integration_worktree(project) / ".ai/contracts.lock").exists()


def test_successful_check_replaces_unknown_exhausted_window(conn):
    providers.observe(conn, "codex", {"available": False, "windows": [
        {"window": "weekly", "used_percent": 100, "resets_at": None}]})
    state = providers.observe(conn, "codex", {"available": True, "complete": True, "windows": []})
    assert state.status == providers.AVAILABLE


def test_renames_audit_both_source_and_destination(project_root):
    from tests.conftest import git
    git(project_root, "mv", "services/retry.py", "services/moved.py")
    assert set(repo.staged_files(project_root)) == {"services/retry.py", "services/moved.py"}
    assert set(repo.changed_files(project_root)) == {"services/retry.py", "services/moved.py"}


def test_git_failure_is_not_a_clean_worktree(tmp_path):
    with pytest.raises(ValueError):
        repo.diff_files(tmp_path, "missing-base-commit")


def test_long_prompts_use_stdin(project, tmp_path, monkeypatch):
    from agentkit.adapters.claude_code import ClaudeCodeAdapter
    from agentkit.adapters.codex import CodexAdapter
    for adapter in (CodexAdapter(), ClaudeCodeAdapter()):
        monkeypatch.setattr(adapter, "detect", lambda: None)
        prompt = "original user intent " * 10000
        launch = adapter.build_launch({}, tmp_path, "coordinator", project, prompt=prompt)
        assert launch.stdin_text == prompt and prompt not in launch.argv
        if adapter.name == "codex":
            assert any("mcp_servers.agentkit.env_vars=" in arg and "AGENTKIT_PROCESS" in arg for arg in launch.argv)


def test_installing_claude_hooks_is_idempotent(tmp_path):
    from agentkit.adapters.claude_code import ClaudeCodeAdapter
    adapter = ClaudeCodeAdapter()
    adapter.install_guards(tmp_path, {}, tmp_path)
    first = (tmp_path / ".claude/settings.local.json").read_text()
    adapter.install_guards(tmp_path, {}, tmp_path)
    assert (tmp_path / ".claude/settings.local.json").read_text() == first


@pytest.mark.parametrize("kind", ["RESEARCH", "REVIEW"])
def test_readonly_kind_cannot_bypass_isolation(kind, project, tmp_path, monkeypatch):
    from agentkit.adapters.claude_code import ClaudeCodeAdapter
    from agentkit.adapters.codex import CodexAdapter
    with pytest.raises(ValueError, match=r"read-only|unknown or reserved"):
        spec.TaskSpec(spec_id="bad", title="Bad", kind=kind, expected_write=["src/**"]).validate()
    for adapter in (CodexAdapter(), ClaudeCodeAdapter()):
        monkeypatch.setattr(adapter, "detect", lambda: None)
        launch = adapter.build_launch({"kind": kind}, tmp_path, "implementer", project, prompt="Read")
        if adapter.name == "codex":
            assert 'sandbox_mode="read-only"' in launch.argv
        else:
            assert "Read,Grep,Glob,ToolSearch,WaitForMcpServers" in launch.argv
