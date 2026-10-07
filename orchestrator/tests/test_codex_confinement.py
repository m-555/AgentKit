"""Codex launch policy must confine writes before functional probing can certify it."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest

from agentkit.adapters.codex import CodexAdapter


def _config(argv: list[str]) -> dict:
    """Parse the actual TOML values Codex receives, including arrays and booleans."""
    return tomllib.loads("\n".join(argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"))


@pytest.fixture()
def adapter(monkeypatch):
    result = CodexAdapter()
    monkeypatch.setattr(result, "detect", lambda: None)
    return result


@pytest.mark.parametrize("resume_token", [None, "saved-worker-session"])
def test_worker_does_not_inherit_shared_writable_roots(adapter, tmp_path, resume_token):
    worktree = tmp_path / "worker checkout"
    worktree.mkdir()
    launch = adapter.build_launch(
        {"id": 4, "generation": 2}, worktree, "implementer",
        SimpleNamespace(root=tmp_path), prompt="Edit the assigned file", resume_token=resume_token,
    )
    overrides = _config(launch.argv)
    # A user config may allow another checkout and a shared temporary directory.
    # Launch overrides must replace all three allowances, even for a resumed thread.
    inherited = {
        "writable_roots": [str(tmp_path / "other-worker")],
        "exclude_tmpdir_env_var": False,
        "exclude_slash_tmp": False,
    }
    effective = {**inherited, **overrides.get("sandbox_workspace_write", {})}
    assert effective["writable_roots"] == []
    assert effective["exclude_tmpdir_env_var"] is True
    assert effective["exclude_slash_tmp"] is True
    assert overrides["sandbox_mode"] == "workspace-write"
    assert overrides["approval_policy"] == "never"
    assert launch.cwd == str(worktree)
    assert "--add-dir" not in launch.argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in launch.argv
    if resume_token:
        assert launch.argv[:4] == ["codex", "exec", "resume", resume_token]
        assert "--cd" not in launch.argv
    else:
        assert launch.argv[launch.argv.index("--cd") + 1] == str(worktree)


@pytest.mark.parametrize("role,kind", [
    ("coordinator", "SAFE_PARALLEL"),
    ("reviewer", "SAFE_PARALLEL"),
    ("architect", "SAFE_PARALLEL"),
    ("researcher", "SAFE_PARALLEL"),
    ("implementer", "RESEARCH"),
    ("implementer", "REVIEW"),
])
@pytest.mark.parametrize("resume_token", [None, "saved-control-session"])
def test_control_sessions_remain_readonly(adapter, tmp_path, role, kind, resume_token):
    launch = adapter.build_launch(
        {"id": 5, "kind": kind}, tmp_path, role, SimpleNamespace(root=tmp_path),
        prompt="Read the exact diff", resume_token=resume_token,
    )
    config = _config(launch.argv)
    assert config["sandbox_mode"] == "read-only"
    assert config["approval_policy"] == "never"
    assert "--add-dir" not in launch.argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in launch.argv


def test_confinement_keeps_model_mcp_and_prompt_binding(adapter, tmp_path):
    selection = {
        "name": "assigned-sol", "provider": "codex", "model": "gpt-6.1-sol",
        "effort": "xhigh", "rank": 1,
    }
    prompt = "Read and implement the assigned acceptance criteria. " * 1000
    launch = adapter.build_launch(
        {"id": 9, "generation": 3, "_model_selection": selection}, tmp_path,
        "implementer", SimpleNamespace(root=tmp_path), prompt=prompt,
    )
    config = _config(launch.argv)
    assert launch.argv[launch.argv.index("--model") + 1] == selection["model"]
    assert config["model_reasoning_effort"] == "xhigh"
    server = config["mcp_servers"]["agentkit"]
    assert server["args"] == ["-m", "agentkit.mcp_server"]
    assert server["required"] is True
    assert {"AGENTKIT_PROCESS", "AGENTKIT_TASK", "AGENTKIT_ROLE"} <= set(server["env_vars"])
    assert launch.env["AGENTKIT_TASK"] == "9"
    assert launch.env["AGENTKIT_GENERATION"] == "3"
    assert launch.env["AGENTKIT_ROOT"] == str(tmp_path)
    assert launch.stdin_text == prompt
    assert prompt not in launch.argv
    assert launch.argv[-1] == "-"
