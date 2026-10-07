"""Claude hooks use worker filesystem paths and the primary lease database."""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from agentkit import db, hooks_cli


@pytest.fixture()
def linked(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    (primary / "src").mkdir(parents=True)
    for name in ("owned.py", "foreign.py"):
        (primary / "src" / name).write_text("old\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.name", "Hook fixture"],
                 ["config", "user.email", "hook@test.invalid"], ["config", "commit.gpgsign", "false"],
                 ["add", "src"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=primary, check=True, capture_output=True)
    worktree, other = tmp_path / "worker", tmp_path / "other-worker"
    for target in (worktree, other):
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(target)],
                       cwd=primary, check=True, capture_output=True)
    (primary / ".ai").mkdir()
    (primary / ".ai/project.yaml").write_text("name: hook-path-test\ngates: {}\n", encoding="utf-8")
    conn = db.connect(primary)
    task = db.create_task(conn, title="worker", owned_paths=["src/owned.py", "src/new.py"],
                          worktree=str(worktree), generation=2, status="RUNNING")
    db.create_task(conn, title="other worker", owned_paths=["src/foreign.py"],
                   worktree=str(other), generation=2, status="RUNNING")
    conn.close()
    for key, value in {"AGENTKIT_ROOT": str(primary), "AGENTKIT_TASK": str(task),
                       "AGENTKIT_GENERATION": "2", "AGENTKIT_PROCESS": "7"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTKIT_WORKTREE", raising=False)
    return primary, worktree, other, task


def invoke(cwd, target, monkeypatch, *, event="pre-tool-use", tool="Edit"):
    tool_input = {"command": target} if tool == "Bash" else {"file_path": target}
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hooks_cli.main([event])


def test_absolute_owned_linked_worktree_file_is_allowed(linked, monkeypatch):
    _, worktree, _, _ = linked
    assert invoke(worktree, str(worktree / "src/owned.py"), monkeypatch) == 0
    assert not (worktree / ".ai/tasks.db").exists()


@pytest.mark.parametrize("location", ["primary", "other", "foreign_file"])
def test_primary_sibling_and_foreign_worker_paths_are_denied(linked, monkeypatch, location):
    primary, worktree, other, _ = linked
    targets = {"primary": primary / "src/owned.py", "other": other / "src/owned.py",
               "foreign_file": worktree / "src/foreign.py"}
    assert invoke(worktree, str(targets[location]), monkeypatch) == 2


@pytest.mark.parametrize("event", ["pre-tool-use", "pre-bash"])
def test_relative_shell_target_uses_validated_payload_subdirectory(linked, monkeypatch, event):
    _, worktree, _, _ = linked
    assert invoke(worktree / "src", "echo allowed > owned.py", monkeypatch, event=event, tool="Bash") == 0
    assert invoke(worktree / "src", "echo breached > foreign.py", monkeypatch, event=event, tool="Bash") == 2


def test_relative_file_and_new_target_use_assigned_cwd(linked, monkeypatch):
    _, worktree, _, _ = linked
    assert invoke(worktree / "src", "owned.py", monkeypatch) == 0
    assert invoke(worktree / "src", "new.py", monkeypatch, tool="Write") == 0
    assert invoke(worktree / "src", "src/owned.py", monkeypatch) == 2


def test_multiedit_requires_every_target_to_belong_to_assigned_worktree(linked, monkeypatch):
    primary, worktree, _, _ = linked
    payload = {"tool_name": "MultiEdit", "cwd": str(worktree), "tool_input": {"edits": [
        {"file_path": str(worktree / "src/owned.py")}, {"file_path": str(primary / "src/owned.py")}]}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.main(["pre-tool-use"]) == 2


@pytest.mark.parametrize("event", ["pre-tool-use", "pre-bash"])
def test_absolute_shell_targets_use_worker_paths_and_primary_authority(linked, monkeypatch, event):
    primary, worktree, other, _ = linked
    for target, expected in ((worktree / "src/owned.py", 0), (primary / "src/owned.py", 2),
                             (other / "src/owned.py", 2), (worktree / "src/foreign.py", 2)):
        assert invoke(worktree, f"echo content > {target.as_posix()}", monkeypatch, event=event, tool="Bash") == expected
    assert not (worktree / ".ai/tasks.db").exists()
    assert not (other / ".ai/tasks.db").exists()


@pytest.mark.parametrize("owned", [[], ["**"]])
def test_shell_outside_worktree_never_uses_permissive_or_broad_lease(linked, monkeypatch, owned):
    primary, worktree, _, task = linked
    conn = db.connect(primary)
    db.update_task(conn, task, owned_paths=owned)
    conn.close()
    assert invoke(worktree, f"echo breached > {(primary / 'src/owned.py').as_posix()}", monkeypatch, tool="Bash") == 2


@pytest.mark.parametrize("invalid", ["generation", "missing_generation", "stale", "task", "unknown_task",
    "missing_task", "task_status", "assignment", "missing_worktree", "relative_worktree", "launcher_worktree",
    "authority", "database", "cwd", "relative_cwd", "missing_cwd", "malformed_cwd"])
@pytest.mark.parametrize("tool", ["Edit", "Bash"])
def test_stale_or_inconsistent_supervised_state_denies_instead_of_failing_open(linked, monkeypatch, invalid, tool, capsys):
    primary, worktree, other, task = linked
    cwd = worktree
    updates: dict[str, str | None] = {}
    if invalid == "generation":
        monkeypatch.setenv("AGENTKIT_GENERATION", "not-an-integer")
    elif invalid == "missing_generation":
        monkeypatch.delenv("AGENTKIT_GENERATION")
    elif invalid == "stale":
        monkeypatch.setenv("AGENTKIT_GENERATION", "1")
    elif invalid == "task":
        monkeypatch.setenv("AGENTKIT_TASK", "invalid")
    elif invalid == "unknown_task":
        monkeypatch.setenv("AGENTKIT_TASK", "999")
    elif invalid == "missing_task":
        monkeypatch.delenv("AGENTKIT_TASK")
    elif invalid == "task_status":
        updates["status"] = "STALE"
    elif invalid == "assignment":
        updates["worktree"] = str(other)
    elif invalid == "missing_worktree":
        updates["worktree"] = None
    elif invalid == "relative_worktree":
        updates["worktree"] = "../worker"
    elif invalid == "launcher_worktree":
        monkeypatch.setenv("AGENTKIT_WORKTREE", str(other))
    elif invalid == "authority":
        monkeypatch.setenv("AGENTKIT_ROOT", str(other))
    elif invalid == "database":
        (primary / ".ai/tasks.db").unlink()
    elif invalid == "cwd":
        cwd = other
    elif invalid == "relative_cwd":
        cwd = "."
    if updates:
        conn = db.connect(primary)
        db.update_task(conn, task, **updates)
        conn.close()
    tool_input = {"command": "echo changed > src/owned.py"} if tool == "Bash" else {"file_path": str(worktree / "src/owned.py")}
    payload = {"cwd": str(cwd), "tool_name": tool, "tool_input": tool_input}
    if invalid == "missing_cwd":
        payload.pop("cwd")
        payload["workspace"] = str(worktree)  # A fallback cannot replace supervised cwd evidence.
    elif invalid == "malformed_cwd":
        payload["cwd"] = [str(worktree)]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks_cli.main(["pre-tool-use"]) == 2
    assert "allowing" not in capsys.readouterr().err
    if invalid == "database":
        assert not (primary / ".ai/tasks.db").exists()


def test_assigned_worker_gate_is_not_trusted_from_another_subdirectory(linked, monkeypatch):
    primary, worktree, _, _ = linked
    (primary / ".ai/project.yaml").write_text('name: hook-path-test\ngates:\n  fast: ["python -m pytest -q"]\n', encoding="utf-8")
    assert invoke(worktree, "python -m pytest -q", monkeypatch, tool="Bash") == 0
    assert invoke(worktree / "src", "python -m pytest -q", monkeypatch, tool="Bash") == 2


def test_manual_task_without_worker_markers_retains_primary_checkout_behavior(linked, monkeypatch):
    primary, _, _, task = linked
    for key in ("AGENTKIT_ROOT", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(key, raising=False)
    conn = db.connect(primary)
    db.update_task(conn, task, worktree=None)
    conn.close()
    assert invoke(primary, str(primary / "src/owned.py"), monkeypatch) == 0


def test_unmanaged_manual_session_and_unexpected_hook_error_keep_fail_open(linked, monkeypatch, capsys):
    _, worktree, _, _ = linked
    for key in ("AGENTKIT_TASK", "AGENTKIT_ROOT", "AGENTKIT_GENERATION", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(hooks_cli, "_context", lambda *args, **kwargs: (None, None))
    assert invoke(worktree, "outside-file.py", monkeypatch) == 0
    def broken(payload):
        raise RuntimeError("unexpected implementation failure")
    monkeypatch.setitem(hooks_cli.HANDLERS, "pre-tool-use", broken)
    assert invoke(worktree, "outside-file.py", monkeypatch) == 0
    assert "allowing" in capsys.readouterr().err
