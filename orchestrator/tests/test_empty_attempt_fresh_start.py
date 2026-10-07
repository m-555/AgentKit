"""A retry whose previous attempt preserved nothing starts fresh.

Ownership, scope and ancestry are still audited before launch. But a stopped
attempt that left its checkout at the recorded base, clean, has no work to
continue: a continuation packet only repeats the brief's next action and can
push a bounded assignment over `max_context_chars` (seen in a pilot task).
"""
from __future__ import annotations

import json

import pytest

from agentkit import adapters, checkpoints, db, models, processes, repo, scheduler, worktrees
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import load_project
from tests.conftest import commit_all
from tests.test_handoff import POLICY, dead_pid


@pytest.fixture
def stopped_empty(project_root, conn, monkeypatch):
    """A backend task whose Claude worker stopped before writing anything."""
    with (project_root / ".ai" / "project.yaml").open("a", encoding="utf-8") as handle:
        handle.write(POLICY)
    commit_all(project_root, "pin models")
    project = load_project(project_root)
    caps = {}
    for name in ("codex", "claude-code"):
        caps[name] = CapabilitySet(adapter=name)
        for key in caps[name].values:
            caps[name].set(key, True)
    save_cache(project_root, caps)
    task_id = db.create_task(conn, spec_id="api", title="API", status="READY", model_assignment="backend",
                             expected_write=["services/retry.py"], owned_paths=["services/retry.py"],
                             adapter="claude-code", model="claude-opus-5-5", generation=1,
                             next_action="Fresh attempt: write services/retry.py from the brief.")
    work, _ = worktrees.ensure(project_root, db.get_task(conn, task_id), project)
    base = repo.head_commit(work)
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    run_id = db.open_worker_run(conn, task_id, 1, "claude-code", str(work))
    db.close_worker_run(conn, run_id, exit_code=1)
    conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,worker_run_id,status,pid,child_pid,"
                 "launch_json,started_at,requested_model) VALUES('worker','claude-code',?,1,?,'FAILED',?,?,?,?,"
                 "'claude-opus-5-5')",
                 (task_id, run_id, dead_pid(), dead_pid(), json.dumps({"argv": ["claude"]}), db.utcnow()))
    checkpoints.write_mechanical(conn, project_root, work, task_id, "worker_exit")
    launched = []

    def start(connection, root, built, **fields):
        launched.append(built)
        return connection.execute("INSERT INTO processes(purpose,provider,task_id,launch_json,started_at) "
                                  "VALUES('worker',?,?,'{}',?)", (fields["provider"], task_id, db.utcnow())).lastrowid

    monkeypatch.setattr(processes, "start", start)
    for name in ("codex", "claude-code"):
        monkeypatch.setattr(adapters.get(name), "detect", lambda: None)
    return {"task": task_id, "work": work, "project": project, "launched": launched, "base": base}


def _launch(conn, project_root, state, selected):
    task = db.get_task(conn, state["task"])
    plan = scheduler.LaunchPlan(task, selected.provider, state["work"], 2, "test", selected.to_dict())
    return scheduler.launch(conn, project_root, state["project"], plan)


def test_an_empty_previous_attempt_relaunches_without_a_continuation_packet(project_root, conn, stopped_empty):
    selected = models.Profile("opus", "claude-code", "claude-opus-5-5", "xhigh", 2)
    ok, detail = _launch(conn, project_root, stopped_empty, selected)
    assert ok, detail
    prompt = stopped_empty["launched"][0].stdin_text
    assert "continuation" not in prompt.lower()
    assert prompt.count("Fresh attempt: write services/retry.py from the brief.") <= 1
    assert db.recent_events(conn, stopped_empty["task"], kind="handoff_audited")


def test_preserved_work_still_gets_its_continuation_packet(project_root, conn, stopped_empty):
    work = stopped_empty["work"]
    (work / "services/retry.py").write_text("VALUE = 'half'\n", encoding="utf-8")
    commit_all(work, "milestone: retry skeleton")
    checkpoints.write_mechanical(conn, project_root, work, stopped_empty["task"], "worker_exit")
    selected = models.Profile("opus", "claude-code", "claude-opus-5-5", "xhigh", 2)
    ok, detail = _launch(conn, project_root, stopped_empty, selected)
    assert ok, detail
    assert "Continuation state" in stopped_empty["launched"][0].stdin_text
