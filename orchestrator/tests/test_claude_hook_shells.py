"""A Claude guard hook keeps its verdict in whichever shell Claude Code runs it.

On Windows, Claude Code runs hooks in Git Bash when it finds it, and in PowerShell
otherwise. Claude Code 2.1.296 chose PowerShell on a machine whose Git lives outside
the default folder. AgentKit wrote `"...python.exe" -m agentkit.hooks_cli ...`, which
PowerShell cannot parse. PowerShell would also turn the guard's exit code 2 (block)
into 1, which does not block. The functional probe then watched a write go through
and, correctly, withheld Claude writers. The hook command now works in both shells
and keeps the exit code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.adapters import claude_code
from agentkit.adapters.claude_code import ClaudeCodeAdapter, hook_command


def installed(tmp_path):
    ClaudeCodeAdapter().install_guards(tmp_path, {"id": 1}, tmp_path)
    settings = json.loads((tmp_path / ".claude/settings.local.json").read_text(encoding="utf-8"))
    return [hook["command"] for event in ("PreToolUse", "PostToolUse")
            for group in settings["hooks"][event] for hook in group["hooks"]]


def test_a_plain_windows_path_works_in_both_shells_and_keeps_the_exit_code(monkeypatch):
    monkeypatch.setattr(claude_code.os, "name", "nt")
    assert hook_command("C:\\venv\\Scripts\\python.exe", "-m agentkit.hooks_cli pre-bash") == \
        "C:/venv/Scripts/python.exe -m agentkit.hooks_cli pre-bash; exit $LASTEXITCODE"


def test_posix_keeps_the_quoted_form(monkeypatch):
    monkeypatch.setattr(claude_code.os, "name", "posix")
    assert hook_command("/opt/venv/bin/python", "-m agentkit.hooks_cli pre-bash") == \
        '"/opt/venv/bin/python" -m agentkit.hooks_cli pre-bash'


def test_a_windows_path_with_spaces_uses_the_shell_claude_code_will_use(monkeypatch):
    monkeypatch.setattr(claude_code.os, "name", "nt")
    monkeypatch.setattr(claude_code, "_git_bash", lambda: None)
    assert hook_command("D:\\Program Files\\Py\\python.exe", "-m x") == \
        '& "D:/Program Files/Py/python.exe" -m x; exit $LASTEXITCODE'
    monkeypatch.setattr(claude_code, "_git_bash", lambda: "C:/Program Files/Git/bin/bash.exe")
    assert hook_command("D:\\Program Files\\Py\\python.exe", "-m x") == '"D:/Program Files/Py/python.exe" -m x'


def test_a_path_that_cannot_be_quoted_safely_is_refused(monkeypatch):
    monkeypatch.setattr(claude_code.os, "name", "nt")
    for bad in ('C:\\a"b\\python.exe', "C:\\a$b\\python.exe", "C:\\a`b\\python.exe"):
        with pytest.raises(ValueError, match="cannot be safely quoted"):
            hook_command(bad, "-m x")


def test_git_bash_is_found_the_way_claude_code_finds_it(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    for path in (git, bash):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: str(git) if name == "git" else None)
    monkeypatch.setattr(claude_code, "_DEFAULT_GIT_BASH", ())
    assert claude_code._git_bash() == str(bash)
    # git.exe from mingw64\bin does not lead Claude Code to bash: it falls back to PowerShell.
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: str(tmp_path / "Git/mingw64/bin/git.exe"))
    assert claude_code._git_bash() is None
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
    assert claude_code._git_bash() == str(bash)


def test_every_installed_guard_uses_the_shell_safe_form(tmp_path):
    commands = installed(tmp_path)
    for argument in ("pre-tool-use", "pre-bash", "post-tool-use"):
        assert hook_command(sys.executable, "-m agentkit.hooks_cli " + argument) in commands
    assert len(commands) == 3


def _git_bash_for_test():
    git = shutil.which("git")
    for parent in (Path(git).parents if git else ()):
        if (parent / "bin" / "bash.exe").is_file():
            return str(parent / "bin" / "bash.exe")
    return claude_code._git_bash()


@pytest.mark.skipif(os.name != "nt", reason="the Git Bash or PowerShell choice is Windows behaviour")
@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_a_block_survives_the_shell_that_runs_the_hook(tmp_path, shell):
    script = tmp_path / "block.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    command = hook_command(sys.executable, str(script).replace("\\", "/"))
    if shell == "powershell":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        bash = _git_bash_for_test()
        if bash is None:
            pytest.skip("Git Bash is not installed")
        argv = [bash, "-c", command]
    assert subprocess.run(argv, capture_output=True, timeout=120).returncode == 2
