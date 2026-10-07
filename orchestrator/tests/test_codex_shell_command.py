"""Actual local shell execution validates the hook process contract without models."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import db
from agentkit.adapters.codex import CodexAdapter, _hook_command
from agentkit.secrets import worker_environment


@pytest.mark.parametrize("kind", ["posix", "powershell"])
def test_hook_command_explicit_shell_syntax_and_exit_propagation(kind):
    command = _hook_command("C:/Program Files/Python/python.exe", shell_kind=kind)
    base = '"C:/Program Files/Python/python.exe" -I -m agentkit.hooks_cli codex-pre-tool-use'
    assert command == (f"& {base}; exit $LASTEXITCODE" if kind == "powershell" else base)


def test_default_hook_shell_has_an_explicit_windows_certification_assumption():
    command = _hook_command(sys.executable)
    assert command.startswith("& ") == (os.name == "nt")
    assert command.endswith("; exit $LASTEXITCODE") == (os.name == "nt")


@pytest.mark.parametrize("kind", ["cmd", "unverified"])
def test_unreported_shell_kind_is_not_claimed_supported(kind):
    with pytest.raises(ValueError, match="Unverified"):
        _hook_command(sys.executable, shell_kind=kind)


@pytest.mark.parametrize("executable", ['C:/unsafe"/python.exe', "C:/$EXPANSION/python.exe", "C:/`bad/python.exe",
                                        "C:/%ENV%/python.exe", "C:/bad\n/python.exe"])
def test_executable_expansion_or_malformed_quoting_is_refused(executable):
    with pytest.raises(ValueError, match="safely quoted"):
        _hook_command(executable, shell_kind="powershell")


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows PowerShell hook execution")
@pytest.mark.parametrize("case", ["owned_patch", "foreign_patch", "foreign_absolute_bash", "malformed_stdin"])
def test_generated_command_preserves_actual_outer_zero_or_two_with_hook_stdin(tmp_path, case):
    shell = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not shell.is_file():
        pytest.skip("Windows PowerShell is unavailable")
    primary, worktree = tmp_path / "primary", tmp_path / "worker"
    (primary / ".ai").mkdir(parents=True)
    (primary / ".ai/project.yaml").write_text("name: shell-contract\ngates: {}\n", encoding="utf-8")
    worktree.mkdir()
    for target in ("owned.txt", "foreign.txt"):
        (worktree / target).write_bytes(b"old\n")
    conn = db.connect(primary)
    task = db.create_task(conn, title="worker", owned_paths=["owned.txt"], worktree=str(worktree),
                          generation=1, status="RUNNING")
    conn.close()
    report = CodexAdapter().install_guards(worktree, {"id": task}, primary)
    value = json.loads((worktree / ".codex/hooks.json").read_text(encoding="utf-8"))
    handler = value["hooks"]["PreToolUse"][0]["hooks"][0]
    env = worker_environment({"AGENTKIT_ROOT": str(primary), "AGENTKIT_WORKTREE": str(worktree),
                              "AGENTKIT_TASK": str(task), "AGENTKIT_GENERATION": "1", "AGENTKIT_CODEX_READONLY": "0"})
    tool = "Bash" if case == "foreign_absolute_bash" else "apply_patch"
    target = "owned.txt" if case == "owned_patch" else "foreign.txt"
    command = (f'echo breached > "{worktree / target}"' if tool == "Bash" else
               f"*** Begin Patch\n*** Update File: {worktree / target}\n@@\n-old\n+new\n*** End Patch")
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(worktree),
               "tool_input": {"command": command}}
    stdin = "not JSON" if case == "malformed_stdin" else json.dumps(payload)
    completed = subprocess.run([str(shell), "-NoProfile", "-Command", handler["command"]], cwd=worktree,
                               env=env, input=stdin, text=True, encoding="utf-8", errors="replace",
                               capture_output=True, timeout=20)
    assert completed.returncode == (0 if case == "owned_patch" else 2), completed.stderr
    if case == "owned_patch":
        assert not completed.stdout.strip() and not completed.stderr.strip()
    else:
        assert json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "blocked" in completed.stderr.lower()
    assert (worktree / "owned.txt").read_bytes() == b"old\n"
    assert (worktree / "foreign.txt").read_bytes() == b"old\n"
    assert not report.has("L3_prewrite_guard") and not report.has("L4_shell_guard")


def test_bounded_powershell_write_primitive_requires_absolute_owned_target(tmp_path):
    from agentkit.codex_hooks import _shell
    from agentkit.leases import Decision
    owned = str(tmp_path / "allowed.txt").replace("\\", "/")
    seen = []
    def authorize(path):
        seen.append(path)
        return Decision(path == owned, "ownership", "test")
    command = f"Set-Content -LiteralPath '{owned}' -Value 'CONTROL_OK' -NoNewline"
    assert _shell(command, authorize, [], reliable_cwd=False).allowed
    assert seen == [owned]
    assert not _shell("Set-Content -LiteralPath 'relative.txt' -Value 'x'", authorize, [], reliable_cwd=False).allowed
    assert not _shell(command + " -Encoding utf8", authorize, [], reliable_cwd=False).allowed
