"""Measured-model enforcement and executable provenance without provider requests."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from agentkit import (
    adapters,
    capabilities,
    db,
    probe,
    processes,
    providers,
    runner,
    sessions,
)
from agentkit.adapters.base import Installation
from agentkit.capabilities import CapabilitySet, save_cache
from agentkit.runtime_identity import identify


@pytest.mark.parametrize("requested,observed,expected", [
    ("gpt-6.1-sol", "gpt-6.1-sol", True),
    ("gpt-6.1-sol", "gpt-6.1-sol-20261001", True),
    ("claude-opus-5-5", "claude-opus-5-5[1m]", True),
    ("gpt-6.1-sol", "gpt-6.1-sol-mini", False),
    ("gpt-6.1-sol", "gpt-6.1-sol[mini]", False),
    ("claude-opus-5-5", "claude-opus-5-5-other-tier", False),
])
def test_exact_model_identity_rejects_weaker_tier_suffixes(requested, observed, expected):
    assert sessions.model_matches(requested, observed) is expected


def test_finished_mismatch_is_rejected_even_when_child_already_exited(project_root, conn, monkeypatch):
    task = db.create_task(conn, title="exact model", status="RUNNING", generation=1, adapter="claude-code", model="claude-opus-5-5")
    payload = {"argv": ["fake-child"], "cwd": str(project_root), "env": {"AGENTKIT_MODEL": "claude-opus-5-5", "AGENTKIT_MODEL_EFFORT": "xhigh"}}
    pid = conn.execute("INSERT INTO processes(purpose,provider,task_id,generation,launch_json,started_at,requested_model,requested_effort) VALUES('worker','claude-code',?,1,?,?,'claude-opus-5-5','xhigh')", (task, json.dumps(payload), db.utcnow())).lastrowid
    class ExitedChild:
        pid = 123456789
        stdin = None
        stdout = io.StringIO(json.dumps({"type": "result", "result": "done", "model": "claude-sonnet-5", "is_error": False}) + "\n")
        stderr = io.StringIO("")
        def poll(self):
            return 0
        def wait(self, **kwargs):
            return 0
        def terminate(self):
            pytest.fail("completed child does not need termination")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: ExitedChild())
    runner.run(project_root, pid)
    record = processes.get(conn, pid)
    assert record["status"] == "FAILED" and record["model_verified"] == 0
    assert record["observed_model"] == "claude-sonnet-5"
    assert db.get_task(conn, task)["status"] == "READY"
    assert db.get_task(conn, task)["attempts"] == 0


@pytest.mark.parametrize("change", ["path", "source", "bytes", "version"])
def test_a_different_executable_cannot_reuse_write_confinement_proof(project_root, tmp_path, monkeypatch, change):
    first = tmp_path / "first.exe"
    first.write_bytes(b"measured executable")
    install = Installation("codex", str(first), "0.159.2", "path")
    caps = CapabilitySet(adapter="codex", version=install.version, installation=identify(install))
    caps.set("workspace_sandbox", True)
    save_cache(project_root, {"codex": caps})
    adapter = adapters.get("codex")
    monkeypatch.setattr(adapter, "detect", lambda: install)
    assert capabilities.cached_runtime_problem(project_root, adapter, "SAFE_PARALLEL") is None
    if change == "path":
        second = tmp_path / "second.exe"
        second.write_bytes(first.read_bytes())
        install.path = str(second)
    elif change == "source":
        install.source = "extension"
    elif change == "version":
        install.version = "0.160.0"
    else:
        first.write_bytes(b"different same-version executable")
    assert "fresh functional confinement probe" in capabilities.cached_runtime_problem(project_root, adapter, "SAFE_PARALLEL")
    assert capabilities.cached_runtime_problem(project_root, adapter, "RESEARCH") is None


def test_read_only_effective_runtime_fails_positive_control(monkeypatch):
    adapter = adapters.get("codex")
    caps = CapabilitySet(adapter="codex")
    caps.set("workspace_sandbox", True)
    monkeypatch.setattr(probe, "_run", lambda *args: 0)
    monkeypatch.setattr(probe, "_run_capture", lambda *args: (0, '{"item":{"type":"command_execution","exit_code":1,"aggregated_output":"read-only file system"}}'))
    result = probe.probe_functional(adapter, Installation("codex", "fake-codex", "0.159.2"), caps)
    assert not result.has("workspace_sandbox") and not result.has("write_worker_safe")
    assert "inconclusive" in result.notes["workspace_sandbox"]


@pytest.mark.parametrize("reported", ["claude-opus-5-5", "claude-sonnet-5", None])
def test_claude_availability_uses_pinned_opus_and_verifies_event(monkeypatch, reported):
    adapter = adapters.get("claude-code")
    monkeypatch.setattr(adapter, "detect", lambda: Installation("claude-code", "fake-claude", "2.1.286"))
    argv = []
    def run(command, **kwargs):
        argv.extend(command)
        init = {"type": "system", "subtype": "init", "model": reported}
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(init) + "\n" + json.dumps({"type": "result", "result": "AGENTKIT_AVAILABLE", "is_error": False}))
    monkeypatch.setattr(__import__("subprocess"), "run", run)
    result = adapter.check_availability()
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert result["available"] is (True if reported == "claude-opus-5-5" else None)
    assert result["complete"] is False


def test_real_session_limit_wording_is_quota_without_attempt_charge(conn):
    adapter = adapters.get("claude-code")
    error = adapter.classify_error("You've hit your session limit ? resets 9pm (Europe/Paris)", 1)
    assert error.kind == "USAGE_LIMIT" and error.retry_at is not None
    from agentkit import quota
    task = db.create_task(conn, title="preserved partial work", status="RUNNING")
    quota.handle_worker_failure(conn, task, "claude-code", "You've hit your session limit ? resets 9pm (Europe/Paris)", 1)
    assert db.get_task(conn, task)["attempts"] == 0 and not providers.is_available(conn, "claude-code")


def test_live_and_manager_cli_dispatch(project_root, monkeypatch):
    from agentkit import cli, live
    args = cli.build_parser().parse_args(["live", "--task", "7", "--follow", "--poll", "1.5", "--json"])
    assert (args.task, args.follow, args.poll, args.json) == (7, True, 1.5, True)
    calls = []
    monkeypatch.setattr(live, "run", lambda root, **kwargs: calls.append((root, kwargs)) or 0)
    assert cli.main(["--path", str(project_root), "live", "--task", "7", "--json"]) == 0
    assert calls[0][1] == {"task_id": 7, "follow": False, "poll_seconds": 2.0, "json_output": True}
    command = cli.build_parser().parse_args(["manager", "attach", "job", "--holder", "native", "--pid", "123", "--ttl", "90"])
    assert command.manager_command == "attach" and command.pid == 123
