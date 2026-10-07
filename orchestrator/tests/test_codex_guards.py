"""Codex hook payload characterization and provider-free lease guard tests."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentkit import codex_hooks, db, hooks_cli
from agentkit.adapters.codex import CodexAdapter
from agentkit.patch_paths import patch_targets


def test_codex_patch_command_requires_a_dedicated_payload_parser():
    payload: dict = {"tool_name": "apply_patch", "tool_input": {"command":
        "*** Begin Patch\n*** Update File: foreign.py\n@@\n-old\n+new\n*** End Patch"}}
    assert hooks_cli._target_paths({"command": payload["tool_input"]["command"]}) == []


def test_codex_shell_command_field_is_the_documented_wire_shape():
    assert hooks_cli._command_text({"command": "echo CONTROL_OK > allowed.txt"}) == "echo CONTROL_OK > allowed.txt"


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    primary, worktree = tmp_path / "primary", tmp_path / "worker"
    for folder in (primary, worktree):
        (folder / "src").mkdir(parents=True)
        (folder / "src/owned.py").write_text("old\n", encoding="utf-8")
        (folder / "src/foreign.py").write_text("old\n", encoding="utf-8")
    (primary / ".ai").mkdir()
    (primary / ".ai/project.yaml").write_text("name: guard-test\ngates: {}\n", encoding="utf-8")
    conn = db.connect(primary)
    task = db.create_task(conn, title="worker", owned_paths=["src/owned.py", "src/moved.py", "src/output.txt"],
                          worktree=str(worktree), generation=2, status="RUNNING")
    db.create_task(conn, title="foreign worker", owned_paths=["src/foreign.py"], status="RUNNING")
    conn.close()
    for key, value in {"AGENTKIT_ROOT": str(primary), "AGENTKIT_WORKTREE": str(worktree),
                       "AGENTKIT_TASK": str(task), "AGENTKIT_GENERATION": "2", "AGENTKIT_PROCESS": "4"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTKIT_CODEX_READONLY", raising=False)
    return primary, worktree, task


def patch(path="src/owned.py", move=None):
    moved = f"*** Move to: {move}\n" if move else ""
    return f"*** Begin Patch\n*** Update File: {path}\n{moved}@@\n-old\n+new\n*** End Patch"


def invoke(worktree, command, monkeypatch, tool="apply_patch", **tool_fields):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(worktree),
               "tool_input": {"command": command, **tool_fields}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hooks_cli.main(["codex-pre-tool-use"])


@pytest.mark.parametrize("header,body", [("Add File", "+new\n"), ("Delete File", ""),
                                        ("Update File", "@@\n-old\n+new\n")])
def test_extracts_each_patch_operation_and_ignores_header_like_file_content(header, body):
    command = f"*** Begin Patch\n*** {header}: src/file.py\n{body}*** End Patch"
    assert patch_targets(command) == ["src/file.py"]
    assert patch_targets("*** Begin Patch\n*** Add File: file.py\n+*** Update File: decoy.py\n*** End Patch") == ["file.py"]


def test_move_source_and_destination_both_require_ownership(managed, monkeypatch, capsys):
    _, worktree, _ = managed
    assert patch_targets(patch(move="src/moved.py")) == ["src/owned.py", "src/moved.py"]
    assert invoke(worktree, patch(move="src/moved.py"), monkeypatch) == 0
    assert invoke(worktree, patch(move="src/foreign.py"), monkeypatch) == 2
    assert invoke(worktree, patch("src/foreign.py", move="src/moved.py"), monkeypatch) == 2
    captured = capsys.readouterr()
    assert "blocked" in captured.err.lower()
    for line in captured.out.splitlines():
        output = json.loads(line)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert output["hookSpecificOutput"]["permissionDecisionReason"]
        assert not {"continue", "stopReason", "suppressOutput", "updatedInput"} & output.keys()


@pytest.mark.parametrize("location", ["worker", "primary", "outside", "traversal"])
def test_absolute_target_authorizes_against_worker_checkout_only(managed, monkeypatch, location):
    primary, worktree, _ = managed
    paths = {"worker": worktree / "src/owned.py", "primary": primary / "src/owned.py",
             "outside": primary.parent / "outside.py", "traversal": worktree / "../primary/src/owned.py"}
    expected = 0 if location == "worker" else 2
    assert invoke(worktree, patch(str(paths[location])), monkeypatch) == expected
    assert not (worktree / ".ai/tasks.db").exists()


def test_real_linked_worktree_uses_primary_database_for_the_relative_lease(managed, monkeypatch):
    primary, worktree, task = managed
    for args in (["init", "-q"], ["config", "user.name", "Hook fixture"],
                 ["config", "user.email", "hook@test.invalid"], ["config", "commit.gpgsign", "false"],
                 ["add", "src"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=primary, check=True, capture_output=True)
    linked = primary.parent / "linked"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(linked)], cwd=primary, check=True, capture_output=True)
    conn = db.connect(primary)
    db.update_task(conn, task, worktree=str(linked))
    conn.close()
    monkeypatch.setenv("AGENTKIT_WORKTREE", str(linked))
    assert invoke(linked, patch(), monkeypatch) == 0
    assert invoke(linked, patch("src/foreign.py"), monkeypatch) == 2
    assert not (linked / ".ai/tasks.db").exists()


@pytest.mark.parametrize("directory_field", ["work_dir", "workdir", "cwd"])
def test_shell_requires_actual_directory_for_relative_writes(managed, monkeypatch, directory_field):
    _, worktree, _ = managed
    assert invoke(worktree / "src", "echo CONTROL_OK > output.txt", monkeypatch, tool="Bash") == 2
    assert invoke(worktree, "echo CONTROL_OK > output.txt", monkeypatch, tool="Bash",
                  **{directory_field: "src"}) == 0
    assert invoke(worktree, "echo BREACHED > foreign.py", monkeypatch, tool="Bash",
                  **{directory_field: "src"}) == 2


def test_shell_outside_and_conflicting_work_directories_are_denied(managed, monkeypatch):
    primary, worktree, _ = managed
    assert invoke(worktree, "echo BREACHED > owned.py", monkeypatch, tool="Bash", work_dir=str(primary / "src")) == 2
    assert invoke(worktree, "echo CONTROL_OK > output.txt", monkeypatch, tool="Bash", work_dir="src", workdir=".") == 2


@pytest.mark.parametrize("command", ["python -c 'open(\"src/owned.py\",\"w\")'", "bash", "git config x y",
    "find . -exec touch src/foreign.py", "tee src/foreign.py", "mv src/foreign.py src/moved.py",
    "echo hi > src/*", "echo hi > src/output.txt; touch src/foreign.py", "rg --pre evil src", "git diff --output=src/foreign.py"])
def test_unproven_or_foreign_shell_writes_are_denied(managed, monkeypatch, command):
    _, worktree, _ = managed
    assert invoke(worktree, command, monkeypatch, tool="Bash") == 2


def test_exact_declared_gate_does_not_allow_suffix_or_wrong_directory(managed, monkeypatch):
    primary, worktree, _ = managed
    (primary / ".ai/project.yaml").write_text('name: guard-test\ngates:\n  fast: ["python -m pytest -q"]\n', encoding="utf-8")
    assert invoke(worktree, "python -m pytest -q", monkeypatch, tool="Bash") == 2
    assert invoke(worktree, "python -m pytest -q", monkeypatch, tool="Bash", workdir=str(worktree)) == 0
    assert invoke(worktree, "python -m pytest -q; touch src/foreign.py", monkeypatch, tool="Bash") == 2
    assert invoke(worktree / "src", "python -m pytest -q", monkeypatch, tool="Bash") == 2


@pytest.mark.parametrize("command", ["", "*** Begin Patch\n*** End Patch", "apply_patch << EOF\n" + patch(),
    "*** Begin Patch\n*** Move to: src/owned.py\n*** End Patch", patch() + "\ntrailing garbage",
    "*** Begin Patch\n*** Update File: src/owned.py\nunknown syntax\n*** End Patch"])
def test_malformed_patch_refuses_to_infer_targets(managed, monkeypatch, command):
    _, worktree, _ = managed
    assert invoke(worktree, command, monkeypatch) == 2


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"hook_event_name":"PreToolUse"}',
    '{"hook_event_name":"PreToolUse","tool_name":"apply_patch","tool_input":"wrong"}'])
def test_malformed_supervised_payloads_use_supported_denial(managed, monkeypatch, raw, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert hooks_cli.main(["codex-pre-tool-use"]) == 2
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("invalid", ["task", "generation", "stale", "database", "cwd", "assignment"])
def test_unavailable_or_inconsistent_authority_is_denied(managed, monkeypatch, invalid):
    primary, worktree, task = managed
    if invalid == "task":
        monkeypatch.setenv("AGENTKIT_TASK", "None")
    elif invalid == "generation":
        monkeypatch.delenv("AGENTKIT_GENERATION")
    elif invalid == "stale":
        monkeypatch.setenv("AGENTKIT_GENERATION", "1")
    elif invalid == "database":
        (primary / ".ai/tasks.db").unlink()
    elif invalid == "cwd":
        worktree = primary
    else:
        monkeypatch.setenv("AGENTKIT_WORKTREE", str(primary))
    assert invoke(worktree, patch(), monkeypatch) == 2


def test_hook_internal_error_denies_without_changing_claude_contract(managed, monkeypatch):
    _, worktree, _ = managed
    def broken(*args):
        raise RuntimeError("lease database failed")
    monkeypatch.setattr(codex_hooks, "load_project", broken)
    assert invoke(worktree, patch(), monkeypatch) == 2
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hooks_cli.main(["pre-tool-use"]) == 0


@pytest.mark.parametrize("target", [".codex/hooks.json", ".codex/config.toml", ".git/config", "src/owned.py:alternate"])
def test_enforcement_control_files_are_not_leasable_through_codex_hook(managed, monkeypatch, target):
    primary, worktree, task = managed
    conn = db.connect(primary)
    db.update_task(conn, task, owned_paths=["**"])
    conn.close()
    assert invoke(worktree, patch(target), monkeypatch) == 2


def test_readonly_control_without_worker_task_can_read_and_cannot_write(managed, monkeypatch):
    _, worktree, _ = managed
    monkeypatch.setenv("AGENTKIT_CODEX_READONLY", "1")
    monkeypatch.delenv("AGENTKIT_TASK")
    monkeypatch.delenv("AGENTKIT_GENERATION")
    assert invoke(worktree, "git --no-pager status --short", monkeypatch, tool="Bash") == 0
    assert invoke(worktree, patch(), monkeypatch) == 2
    assert invoke(worktree, "echo write > src/owned.py", monkeypatch, tool="Bash") == 2


def test_unmanaged_session_skips_codex_enforcement(monkeypatch):
    for key in ("AGENTKIT_TASK", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not JSON"))
    assert hooks_cli.main(["codex-pre-tool-use"]) == 0


def test_scoped_hook_generation_preserves_existing_definitions_and_never_grants_trust(tmp_path, monkeypatch):
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "detect", lambda: None)
    settings = tmp_path / ".codex"
    settings.mkdir()
    previous = {"matcher": "^Read$", "hooks": [{"type": "command", "command": "trusted-existing-hook"}]}
    (settings / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [previous]}}), encoding="utf-8")
    report = adapter.install_guards(tmp_path, {"id": 3}, tmp_path)
    adapter.install_guards(tmp_path, {"id": 3}, tmp_path)
    value = json.loads((settings / "hooks.json").read_text())
    assert value["hooks"]["PreToolUse"][0] == previous
    assert len(value["hooks"]["PreToolUse"]) == 2
    group = value["hooks"]["PreToolUse"][1]
    assert group["matcher"] == "^(apply_patch|Bash)$"
    handler = group["hooks"][0]
    assert handler["type"] == "command" and handler["timeout"] == 20
    assert "-I -m agentkit.hooks_cli codex-pre-tool-use" in handler["command"] and not handler.get("async")
    assert not report.has("L3_prewrite_guard") and not report.has("L4_shell_guard")
    assert "/hooks" in report.inactive["L3_prewrite_guard"]
    assert "state" not in value and "trusted_hash" not in json.dumps(value)
    for role in ("implementer", "reviewer"):
        launch = adapter.build_launch({"id": 3}, tmp_path, role, SimpleNamespace(root=tmp_path), prompt="fixture")
        assert "--dangerously-bypass-hook-trust" not in launch.argv
        assert launch.env["AGENTKIT_WORKTREE"] == str(tmp_path.resolve())
        assert launch.env["AGENTKIT_CODEX_READONLY"] == ("1" if role == "reviewer" else "0")


def test_malformed_existing_hook_file_is_preserved(tmp_path):
    path = tmp_path / ".codex/hooks.json"
    path.parent.mkdir()
    path.write_text('{"hooks":[]}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        CodexAdapter().install_guards(tmp_path, {"id": 3}, tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("command,expected", [("git add -- src/owned.py", 2),
    ("git add -- src/foreign.py", 2), ("git add .", 2), ("git add -A", 2),
    ("git add src/*", 2), ("git commit -a -m update", 2),
    ("git commit --no-verify -m update", 2), ("git commit --amend -m update", 2)])
def test_git_stage_is_explicit_and_commit_never_bypasses_hooks(managed, monkeypatch, command, expected):
    _, worktree, _ = managed
    assert invoke(worktree, command, monkeypatch, tool="Bash") == expected


def test_commit_audits_the_real_index_and_refuses_foreign_staging(managed, monkeypatch):
    _, worktree, _ = managed
    for args in (["init", "-q"], ["config", "user.name", "Hook fixture"],
                 ["config", "user.email", "hook@test.invalid"], ["config", "commit.gpgsign", "false"],
                 ["add", "src"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)
    (worktree / "src/owned.py").write_text("leased update\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/owned.py"], cwd=worktree, check=True, capture_output=True)
    assert invoke(worktree, f'git -C "{worktree}" commit -m update > "{worktree / "src/foreign.py"}"',
                  monkeypatch, tool="Bash") == 2
    assert invoke(worktree, f'git -C "{worktree}" commit -m update', monkeypatch, tool="Bash") == 0
    (worktree / "src/foreign.py").write_text("foreign update\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "src/foreign.py"], cwd=worktree, check=True, capture_output=True)
    assert invoke(worktree, f'git -C "{worktree}" commit -m update', monkeypatch, tool="Bash") == 2


def test_hidden_actual_cwd_never_authorizes_same_basename_elsewhere(managed, monkeypatch):
    _, worktree, _ = managed
    hidden_actual = worktree / "foreign-dir"
    hidden_actual.mkdir()
    (hidden_actual / "owned.py").write_text("foreign content", encoding="utf-8")
    # Installed Codex supplies session cwd=src even when actual workdir differs.
    assert invoke(worktree / "src", "echo breached > owned.py", monkeypatch, tool="Bash") == 2
    absolute = worktree / "src/owned.py"
    assert invoke(worktree / "src", f'echo allowed > "{absolute}"', monkeypatch, tool="Bash") == 0
    assert invoke(worktree / "src", f'echo breached > "{hidden_actual / "owned.py"}"', monkeypatch, tool="Bash") == 2


def test_git_mutations_pin_worktree_and_absolute_staging_paths(managed, monkeypatch):
    primary, worktree, _ = managed
    prefix = f'git -C "{worktree}"'
    assert invoke(worktree, f'{prefix} add -- "{worktree / "src/owned.py"}"', monkeypatch, tool="Bash") == 0
    for command in (f'{prefix} add -- src/owned.py', f'git -C "{primary}" add -- "{worktree / "src/owned.py"}"',
                    f'git -C . add -- "{worktree / "src/owned.py"}"', f'{prefix} -c core.hooksPath=evil commit -m update'):
        assert invoke(worktree, command, monkeypatch, tool="Bash") == 2


@pytest.mark.parametrize("command", ["git log", "git show", "git diff", "bash -i", "pwsh -NoExit", "cmd", "python"])
def test_pager_and_persistent_interactive_shell_channels_are_denied(managed, monkeypatch, command):
    _, worktree, _ = managed
    assert invoke(worktree, command, monkeypatch, tool="Bash") == 2
    assert invoke(worktree, "git --no-pager log -1", monkeypatch, tool="Bash") == 0


def test_interactive_input_is_denied_if_future_cli_emits_a_pretool_event(managed, monkeypatch):
    _, worktree, _ = managed
    assert invoke(worktree, "touch src/foreign.py", monkeypatch, tool="write_stdin") == 2


def test_hook_isolated_python_import_ignores_worker_package_shadowing(managed, monkeypatch):
    _, worktree, _ = managed
    shadow = worktree / "agentkit"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('raise RuntimeError("worker import shadow executed")', encoding="utf-8")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "cwd": str(worktree),
               "tool_input": {"command": patch()}}
    result = subprocess.run([sys.executable, "-I", "-m", "agentkit.hooks_cli", "codex-pre-tool-use"],
                            cwd=worktree, input=json.dumps(payload), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "worker import shadow" not in result.stderr
