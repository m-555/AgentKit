"""Cross-provider continuation: liveness, scope, ancestry, attempts and audit trail."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agentkit import (
    adapters,
    checkpoints,
    db,
    handoff,
    models,
    policy,
    processes,
    repo,
    runner,
    scheduler,
    sessions,
    worktrees,
)
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.config import load_project
from tests.conftest import commit_all

POLICY = """
model_policy:
  assignments:
    backend:
      profile: opus
      model: claude-opus-5-5
      effort: xhigh
      fallback:
        - {profile: sol, model: gpt-6.1-sol, effort: xhigh, when: [USAGE_LIMIT]}
"""


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def paused(project_root, conn, monkeypatch):
    """A backend task whose Claude worker hit its usage limit mid-task."""
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
                             adapter="claude-code", model="claude-opus-5-5", generation=1)
    work, _ = worktrees.ensure(project_root, db.get_task(conn, task_id), project)
    base = repo.head_commit(work)
    (work / "services/retry.py").write_text("VALUE = 'half'\n", encoding="utf-8")
    commit_all(work, "milestone: retry skeleton")
    (work / "services/retry.py").write_text("VALUE = 'three quarters'\n", encoding="utf-8")
    db.update_task(conn, task_id, worktree=str(work), branch=repo.current_branch(work), base_sha=base,
                   session_token="claude-session-123")
    db.write_checkpoint(conn, task_id, {"decisions": ["Keep retry state in memory"], "next_action": "Add backoff",
                                        "completed": ["skeleton"]}, kind="semantic", head_sha=repo.head_commit(work))
    db.record_gate(conn, task_id, "fast", repo.head_commit(work), True, "fast passed")
    launch = {"argv": ["claude"], "cwd": str(work), "env": {"AGENTKIT_MODEL": "claude-opus-5-5"}}
    run_id = db.open_worker_run(conn, task_id, 1, "claude-code", str(work))
    db.close_worker_run(conn, run_id, exit_code=1)
    conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,worker_run_id,status,pid,child_pid,"
                 "session_token,launch_json,started_at,requested_model) VALUES('worker','claude-code',?,1,?,'FAILED',?,?,"
                 "'claude-session-123',?,?,'claude-opus-5-5')",
                 (task_id, run_id, dead_pid(), dead_pid(), json.dumps(launch), db.utcnow()))
    checkpoints.write_mechanical(conn, project_root, work, task_id, "worker_exit")
    policy.record_trigger(conn, task_id, "USAGE_LIMIT", "claude-code", "claude-opus-5-5")
    from datetime import UTC, datetime, timedelta

    from agentkit import providers
    providers.begin_cooldown(conn, "claude-code", reason="Claude subscription allowance exhausted",
                             retry_at=datetime.now(UTC) + timedelta(hours=4))
    launched = []

    def start(connection, root, built, **fields):
        launched.append(built)
        return connection.execute("INSERT INTO processes(purpose,provider,task_id,launch_json,started_at) "
                                  "VALUES('worker',?,?,'{}',?)", (fields["provider"], task_id, db.utcnow())).lastrowid
    monkeypatch.setattr(processes, "start", start)
    for name in ("codex", "claude-code"):
        monkeypatch.setattr(adapters.get(name), "detect", lambda: None)
    return {"task": task_id, "work": work, "project": project, "launched": launched, "base": base}


def launch(conn, project_root, paused, profile="sol"):
    project = paused["project"]
    task = db.get_task(conn, paused["task"])
    selected = models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1) if profile == "sol" else \
        models.Profile("opus", "claude-code", "claude-opus-5-5", "xhigh", 2)
    plan = scheduler.LaunchPlan(task, selected.provider, paused["work"], 2, "test", selected.to_dict())
    return scheduler.launch(conn, project_root, project, plan)


def test_cross_provider_continuation_preserves_work_and_starts_fresh(project_root, conn, paused):
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (paused["task"], repo.head_commit(paused["work"]), "PASS", "earlier", "old evidence", db.utcnow()))
    head = repo.head_commit(paused["work"])
    ok, detail = launch(conn, project_root, paused)
    assert ok, detail
    built = paused["launched"][0]
    assert "resume" not in built.argv and "claude-session-123" not in " ".join(built.argv)
    assert built.env["AGENTKIT_MODEL"] == "gpt-6.1-sol" and built.env["AGENTKIT_MODEL_EFFORT"] == "xhigh"
    for text in ("Cross-provider continuation", "claude-code / claude-opus-5-5", "USAGE_LIMIT",
                 "Keep retry state in memory", "Checks recorded", "services/retry.py", "Add backoff"):
        assert text in built.stdin_text, text
    task = db.get_task(conn, paused["task"])
    assert task["attempts"] == 0 and task["base_sha"] == paused["base"] and task["session_token"] is None
    assert repo.head_commit(paused["work"]) == head
    assert (paused["work"] / "services/retry.py").read_text() == "VALUE = 'three quarters'\n"
    [record] = handoff.history(conn, paused["task"])
    assert (record["source_provider"], record["source_model"], record["target_provider"], record["target_model"],
            record["trigger"]) == ("claude-code", "claude-opus-5-5", "codex", "gpt-6.1-sol", "USAGE_LIMIT")
    assert json.loads(record["evidence"])["audit"]["clean"]
    latest = conn.execute("SELECT verdict FROM reviews WHERE task_id=? ORDER BY id DESC", (paused["task"],)).fetchone()
    assert latest["verdict"] == "INVALIDATED"
    assert db.recent_events(conn, paused["task"], kind="worker_handoff")


@pytest.mark.parametrize("hazard", ["monitor", "child", "active", "open_run"])
def test_continuation_waits_until_the_old_worker_is_provably_gone(project_root, conn, paused, hazard):
    row = conn.execute("SELECT id, worker_run_id FROM processes WHERE task_id=?", (paused["task"],)).fetchone()
    if hazard == "monitor":
        processes.update(conn, row["id"], pid=os.getpid())
    elif hazard == "child":
        processes.update(conn, row["id"], child_pid=os.getpid())
    elif hazard == "active":
        processes.update(conn, row["id"], status="RUNNING")
    else:
        conn.execute("UPDATE worker_runs SET ended_at=NULL WHERE id=?", (row["worker_run_id"],))
    ok, detail = launch(conn, project_root, paused)
    assert not ok and ("continuation refused" in detail or "already has a session" in detail)
    if hazard == "active":
        live = db.get_task(conn, paused["task"])
        continuation = handoff.verify(conn, paused["project"], live, paused["work"],
                                      models.Profile("sol", "codex", "gpt-6.1-sol", "xhigh", 1))
        assert not continuation.ok and "still RUNNING" in continuation.reason
    assert paused["launched"] == [] and db.active_leases(conn) == []
    assert db.get_task(conn, paused["task"])["generation"] == 1


def test_drift_after_the_exit_checkpoint_is_refused(project_root, conn, paused):
    (paused["work"] / "services/retry.py").write_text("VALUE = 'someone else'\n", encoding="utf-8")
    commit_all(paused["work"], "unrecorded commit")
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "HEAD moved" in detail


def test_out_of_scope_preserved_changes_are_refused(project_root, conn, paused):
    (paused["work"] / "services/media.py").write_text("VALUE = 'not mine'\n", encoding="utf-8")
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "outside the task scope" in detail


def test_broken_ancestry_is_refused(project_root, conn, paused):
    db.update_task(conn, paused["task"], base_sha="0" * 40)
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "recorded base" in detail


def test_competing_owner_is_refused(project_root, conn, paused):
    task = db.get_task(conn, paused["task"])
    db.create_task(conn, spec_id="rival", title="Rival", status="REVIEW", branch=task["branch"],
                   expected_write=["services/other.py"])
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "also records this branch" in detail


def test_same_provider_continuation_resumes_its_own_session(project_root, conn, paused):
    from agentkit import providers
    providers.clear_cooldown(conn, "claude-code")
    ok, detail = launch(conn, project_root, paused, profile="opus")
    assert ok, detail
    built = paused["launched"][0]
    assert built.argv[built.argv.index("--resume") + 1] == "claude-session-123"
    assert handoff.history(conn, paused["task"]) == []


@pytest.mark.parametrize("trigger,expected", [("USAGE_LIMIT", "READY"), ("AUTH_ERROR", "BLOCKED")])
def test_supervisor_releases_paused_work_only_for_approved_fallbacks(project_root, conn, paused, trigger, expected):
    from agentkit import providers, quota, supervisor
    if trigger == "AUTH_ERROR":
        providers.observe(conn, "claude-code", {"available": False, "auth_error": True, "reason": "please run /login"})
    policy.record_trigger(conn, paused["task"], trigger, "claude-code", "claude-opus-5-5")
    db.set_status(conn, paused["task"], "BLOCKED")
    quota.pause(conn, paused["task"], provider="claude-code", retry_at=None)
    supervisor.handoff_waiting(conn, project_root)
    task = db.get_task(conn, paused["task"])
    assert task["status"] == expected and task["attempts"] == 0


def test_requested_and_observed_models_are_kept_apart(conn):
    launch_json = json.dumps({"argv": ["claude", "--api-key", "sk-ant-api03-" + "a" * 40], "cwd": ".",
                              "env": {"AGENTKIT_MODEL": "claude-opus-5-5", "AGENTKIT_MODEL_EFFORT": "xhigh",
                                      "AGENTKIT_MODEL_PROFILE": "opus", "ANTHROPIC_API_KEY": "secret-value"}})
    identifier = conn.execute("INSERT INTO processes(purpose,provider,launch_json,started_at,requested_model,"
                              "requested_effort) VALUES('review','claude-code',?,?,'claude-opus-5-5','xhigh')",
                              (launch_json, db.utcnow())).lastrowid
    view = sessions.describe(processes.get(conn, identifier))
    assert view["model_verification"].startswith("requested; provider did not report")
    assert "secret-value" not in json.dumps(view) and "a" * 40 not in json.dumps(view)
    assert view["env"] == {"AGENTKIT_MODEL": "claude-opus-5-5", "AGENTKIT_MODEL_EFFORT": "xhigh",
                           "AGENTKIT_MODEL_PROFILE": "opus"}
    assert sessions.observe(conn, processes.get(conn, identifier), {"model": "claude-opus-5-5-20260915"}) is None
    assert sessions.describe(processes.get(conn, identifier))["model_verification"] == "verified by provider event"
    mismatch = sessions.observe(conn, processes.get(conn, identifier), {"model": "claude-sonnet-5"})
    assert "not available" in mismatch and "claude-sonnet-5" in mismatch


def test_monitor_stops_a_session_that_reports_a_different_model(project_root, conn, project, tmp_path):
    task_id = db.create_task(conn, title="Pinned", status="RUNNING", adapter="claude-code", generation=1,
                             model="claude-opus-5-5")
    script = tmp_path / "fake_claude.py"
    script.write_text("import json, time\n"
                      "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's1', "
                      "'model': 'claude-sonnet-5'}), flush=True)\n"
                      "time.sleep(60)\n", encoding="utf-8")
    launch_json = json.dumps({"argv": [sys.executable, str(script)], "cwd": str(project_root),
                              "env": {"AGENTKIT_MODEL": "claude-opus-5-5"}, "stdin_text": None})
    identifier = conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at,"
                              "requested_model) VALUES('worker','claude-code',?,1,?,?,'claude-opus-5-5')",
                              (task_id, launch_json, db.utcnow())).lastrowid
    runner.run(project_root, identifier)
    record = processes.get(conn, identifier)
    assert record["observed_model"] == "claude-sonnet-5" and record["model_verified"] == 0
    assert record["status"] == "FAILED"
    task = db.get_task(conn, task_id)
    assert task["status"] == "READY" and task["attempts"] == 0
    assert policy.trigger(conn, task_id) == "MODEL_UNAVAILABLE"
    assert not models.usable(conn, project, models.profile(project, "opus"))


@pytest.mark.parametrize("hazard", ["unstaged", "staged", "delete", "untracked", "missing"])
def test_dirty_byte_drift_or_missing_exit_checkpoint_is_refused(project_root, conn, paused, hazard):
    if hazard == "missing":
        conn.execute("DELETE FROM checkpoints WHERE task_id=? AND kind='mechanical'", (paused["task"],))
    elif hazard == "delete":
        (paused["work"] / "services/retry.py").unlink()
    elif hazard == "untracked":
        db.update_task(conn, paused["task"], expected_write=["services/**"], owned_paths=["services/**"])
        (paused["work"] / "services/new.py").write_text("NEW = 1\n")
    else:
        (paused["work"] / "services/retry.py").write_text("VALUE = 'drift'\n")
        if hazard == "staged":
            subprocess.run(["git", "add", "services/retry.py"], cwd=paused["work"], check=True)
    ok, detail = launch(conn, project_root, paused)
    assert not ok and ("bytes" in detail or "byte evidence" in detail)
    assert paused["launched"] == []


def test_fallback_ownership_survives_source_reset_and_parking(project_root, conn, paused):
    from agentkit import providers
    ok, detail = launch(conn, project_root, paused)
    assert ok, detail
    providers.clear_cooldown(conn, "claude-code")
    policy.clear_trigger(conn, paused["task"])
    task = db.get_task(conn, paused["task"])
    selected = models.allowed_workers(conn, paused["project"], task)
    assert [(p.provider, p.model, p.effort) for p in selected] == [("codex", "gpt-6.1-sol", "xhigh")]
    db.update_task(conn, task["id"], blocked_meta=None)
    assert models.allowed_workers(conn, paused["project"], db.get_task(conn, task["id"])) == selected


def test_sticky_transfer_is_durable_before_a_new_process_starts(project_root, conn, paused, monkeypatch):
    from agentkit import providers
    original = processes.start
    def start(connection, root, built, **fields):
        check = db.connect(project_root)
        try:
            assert handoff.history(check, paused["task"])
            providers.clear_cooldown(check, "claude-code")
            policy.clear_trigger(check, paused["task"])
            selected = models.allowed_workers(check, paused["project"], db.get_task(check, paused["task"]))
            assert [(p.provider, p.model, p.effort) for p in selected] == [("codex", "gpt-6.1-sol", "xhigh")]
        finally:
            check.close()
        return original(connection, root, built, **fields)
    monkeypatch.setattr(processes, "start", start)
    ok, detail = launch(conn, project_root, paused)
    assert ok, detail


@pytest.mark.parametrize("token", [None, "known-session"])
def test_missing_child_identity_never_proves_previous_worker_death(project_root, conn, paused, token):
    row = conn.execute("SELECT id FROM processes WHERE task_id=?", (paused["task"],)).fetchone()
    processes.update(conn, row["id"], child_pid=None, session_token=token, child_launch_state="SPAWNING", exit_code=None)
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "already has a session" in detail


@pytest.mark.parametrize("identity", ["reused", "unknown", "old"])
def test_closed_run_pid_reuse_requires_birth_evidence(project_root, conn, paused, monkeypatch, identity):
    from datetime import datetime, timedelta

    from agentkit import process_identity
    run = db.latest_worker_run(conn, paused["task"])
    conn.execute("UPDATE worker_runs SET pid=? WHERE id=?", (os.getpid(), run["id"]))
    ended = datetime.fromisoformat(run["ended_at"])
    current = None if identity == "unknown" else {
        "kind": "windows", "created": 123,
        "born": (ended + timedelta(seconds=10) if identity == "reused" else ended - timedelta(seconds=10)).isoformat(),
    }
    monkeypatch.setattr(process_identity, "fingerprint", lambda pid: current)
    ok, detail = launch(conn, project_root, paused)
    assert ok == (identity == "reused"), detail


def test_separate_retry_does_not_duplicate_brief_summary(project_root, conn, paused):
    with (project_root / ".ai/project.yaml").open("a", encoding="utf-8") as f:
        f.write("\nworkflow:\n  mode: separate-tasks\n")
    task = db.get_task(conn, paused["task"])
    continuation = handoff.Continuation(True, "stopped", {"uncommitted": []})
    packet = handoff.packet(conn, project_root, paused["work"], task, continuation)
    assert "Keep retry state in memory" not in packet
    assert "Branch:" in packet and "Checks recorded:" in packet
    from agentkit import briefs
    brief = briefs.render(briefs.build(conn, load_project(project_root), task["id"]))
    assert "Keep retry state in memory" in brief


@pytest.mark.parametrize("change", ["hold", "scope"])
def test_launch_revalidates_after_host_preparation(project_root, conn, paused, monkeypatch, change):
    from agentkit import environment_prepare, gates
    before = db.get_task(conn, paused["task"])
    runs_before = conn.execute("SELECT count(*) FROM worker_runs").fetchone()[0]

    def changed_during_prepare(*args, **kwargs):
        if change == "hold":
            db.set_status(conn, paused["task"], "NEEDS_REPLAN")
        else:
            db.update_task(conn, paused["task"], spec_hash="new-definition")
        return gates.GateResult("setup", True)

    monkeypatch.setattr(environment_prepare, "prepare", changed_during_prepare)
    ok, detail = launch(conn, project_root, paused)
    assert not ok and "changed during preparation" in detail
    assert paused["launched"] == []
    assert db.get_task(conn, paused["task"])["generation"] == before["generation"]
    assert conn.execute("SELECT count(*) FROM worker_runs").fetchone()[0] == runs_before
    assert not db.active_leases(conn)
