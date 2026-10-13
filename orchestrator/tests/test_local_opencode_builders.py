"""F22: local Qwen builders under measured tool-only confinement.

OpenCode on Windows has no OS sandbox, so it can never prove workspace_sandbox,
and the probe deliberately strips tool guards when it measures that. But a
local worker launched with no shell tool has no way to run code: its only write
paths are OpenCode's path-checked file tools and AgentKit's host-guarded MCP
tools. That boundary is a different, real one. It is measured by the probe
(guarded write denied, outside write denied, no shell offered), named
tool_only_confinement, and it admits bounded builders only: hotspot, decouple
and contract work still need an OS sandbox.
"""
from __future__ import annotations

import json
import sys

import pytest

from agentkit import db, probe
from agentkit.adapters.base import GuardReport, Installation, Launch
from agentkit.adapters.local_opencode import LocalOpenCodeAdapter
from agentkit.capabilities import CapabilitySet
from agentkit.probe_evidence import opencode_denial

RULE = "The user has specified a rule which prevents you from using this specific tool call."


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    root = tmp_path / "local-opencode"
    root.mkdir()
    data = {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:8080/v1"},
                                   "models": {"qwen3.8-27b-q8-tuber": {}, "other-model": {}}}}}
    (root / "opencode.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENTKIT_LOCAL_OPENCODE", str(root))
    return root


def _config(project, **task):
    adapter = LocalOpenCodeAdapter()
    adapter.detect = lambda: None
    base = {"id": 9, "kind": "SAFE_PARALLEL", "role": "backend-builder", "complexity": "easy",
            "model_profile": "qwen", "title": "Resolve stills", "description": "Build the resolver.",
            "expected_write": ["backend/run_stills.py"], "expected_read": ["backend/run_store.py"]}
    launch = adapter.build_launch({**base, **task}, project.root, "backend-builder", project, prompt="Go")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    return config, config["permission"]


def test_a_builder_may_edit_only_its_own_files_and_has_no_shell(project, local_source):
    config, permission = _config(project)
    assert permission["edit"]["*"] == "deny"
    assert [key for key, value in permission["edit"].items() if value == "allow"] == [
        "*backend/run_stills.py", "*backend\\run_stills.py"]
    assert permission.get("bash", "deny") == "deny" and permission["*"] == "deny"
    assert permission["external_directory"] == "deny"
    assert permission["read"]["*backend/run_store.py"] == "allow" and permission["read"]["*"] == "deny"
    for tool in ("agentkit_task_commit", "agentkit_gate_run", "agentkit_checkpoint", "agentkit_task_status"):
        assert permission[tool] == "allow"
    system = config["agent"]["agentkit-worker"]["prompt"]
    assert "Write only: backend/run_stills.py" in system and "agentkit_task_commit" in system
    assert "no shell" in system


@pytest.mark.parametrize("task", [{"kind": "HOTSPOT"}, {"kind": "CONTRACT_CHANGE"}, {"kind": "DECOUPLE"},
                                  {"expected_write": []}])  # any complexity is allowed since 2026-10-10
def test_work_that_needs_an_os_sandbox_or_has_no_scope_is_refused(project, local_source, task):
    with pytest.raises(ValueError):
        _config(project, **task)


def test_research_stays_read_only(project, local_source):
    _, permission = _config(project, kind="RESEARCH", role="researcher", complexity="easy", expected_write=[])
    assert "allow" not in json.dumps(permission.get("edit", "deny"))
    assert "agentkit_task_commit" not in permission


def test_a_probe_launch_uses_the_fixture_lease_and_the_local_qwen_model(tmp_path, local_source):
    workdir = tmp_path / "fixture"
    (workdir / ".ai").mkdir(parents=True)
    conn = db.connect(workdir)
    task_id = db.create_task(conn, title="probe", owned_paths=["allowed.txt"], status="RUNNING", generation=1)
    conn.close()
    adapter = LocalOpenCodeAdapter()
    adapter.detect = lambda: None
    launch = adapter.build_launch({"id": task_id, "generation": 1}, workdir, "implementer",
                                  probe._ProbeProject(workdir), prompt="Probe")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert [k for k, v in config["permission"]["edit"].items() if v == "allow"] == ["*allowed.txt"]
    assert config["model"] == "local/qwen3.8-27b-q8-tuber"
    # The probe observes the tool layer, so the model gets no lease tools to decline with first.
    assert "mcp" not in config and not [k for k in config["permission"] if k.startswith("agentkit_")]
    assert "refus" in config["agent"]["agentkit-worker"]["prompt"]


def test_tool_only_confinement_admits_bounded_builders_but_not_hotspots():
    caps = CapabilitySet(adapter="local-opencode")
    for name in ("tool_only_confinement", "prewrite_file_guard", "resume_session"):
        caps.set(name, True)
    assert caps.derived()["write_worker_safe"] is True
    assert caps.derived()["strong_write_isolation"] is False
    assert caps.missing_for("SAFE_PARALLEL") == [] and caps.missing_for("HOTSPOT")


def _event(tool, path, error=None):
    state = {"status": "error" if error else "completed", "input": {"filePath": path}}
    if error:
        state["error"] = error
    return json.dumps({"type": "tool_use", "part": {"tool": tool, "state": state}})


def test_an_opencode_denial_must_name_the_attempted_target():
    output = "\n".join([_event("edit", "C:\\w\\allowed.txt"), _event("write", "C:\\w-outside.txt", RULE)])
    assert opencode_denial(output, "C:\\w-outside.txt")
    assert not opencode_denial(output, "C:\\w\\guarded.txt")
    assert not opencode_denial(_event("write", "C:\\w-outside.txt", "disk full"), "C:\\w-outside.txt")


SCRIPT = r"""
import json, os, sys
prompt = sys.stdin.read()
work = os.getcwd()
open(os.path.join(work, "allowed.txt"), "w").write("CONTROL_OK")
target = os.environ["PROBE_TARGET_GUARDED"] if "guarded.txt" in prompt else os.environ["PROBE_TARGET_OUTSIDE"]
state = {"status": "error", "input": {"filePath": target},
         "error": "The user has specified a rule which prevents you from using this specific tool call."}
if os.environ.get("PROBE_BREACH") and target.endswith("-outside.txt"):
    open(target, "w").write("BREACHED")
    state = {"status": "completed", "input": {"filePath": target}}
print(json.dumps({"type": "tool_use", "part": {"tool": "write", "state": state}}))
"""


class _ToolOnly:
    name = "local-opencode"
    offers_shell = False

    def __init__(self, breach, declines_guarded=False):
        self.breach = breach
        self.declines_guarded = declines_guarded

    def install_guards(self, worktree, task, orchestrator):
        return GuardReport(active={"L2_permissions"})

    def build_launch(self, task, worktree, role, project, *, prompt, resume_token=None):
        env = {"PROBE_TARGET_GUARDED": str(worktree / "guarded.txt"),
               "PROBE_TARGET_OUTSIDE": str(worktree.parent / (worktree.name + "-outside.txt"))}
        if self.breach:
            env["PROBE_BREACH"] = "1"
        if self.declines_guarded:
            env["PROBE_TARGET_GUARDED"] = str(worktree / "allowed.txt")  # never attempts guarded.txt
        return Launch(argv=[sys.executable, "-c", SCRIPT], env=env, cwd=str(worktree), stdin_text=prompt)


@pytest.mark.parametrize("breach", [False, True])
def test_the_probe_measures_tool_only_confinement(tmp_path, breach):
    caps = probe.probe_functional(_ToolOnly(breach), Installation("local-opencode", sys.executable, "test"),
                                  CapabilitySet(adapter="local-opencode"), fixture_root=tmp_path)
    assert caps.has("workspace_sandbox") is False  # no OS sandbox is ever claimed
    assert caps.has("shell_guard") is False and "no shell" in caps.notes["shell_guard"]
    assert caps.has("prewrite_file_guard") is True
    assert caps.has("tool_only_confinement") is (not breach)
    assert caps.notes["tool_only_confinement"].startswith("functional v2:")


QWEN = {"profile": "qwen", "model": "local/qwen3.8-27b-q8-tuber", "effort": None}


@pytest.mark.parametrize("kind,allowed", [("RESEARCH", True), ("SAFE_PARALLEL", True), ("DEPENDENT", True),
                                          ("TEST_ONLY", True), ("REVIEW", False), ("OPERATOR", False)])
def test_the_model_policy_admits_exactly_the_kinds_the_adapter_can_confine(project, kind, allowed):
    from agentkit import policy
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    task = {"id": 1, "kind": kind, "complexity": "easy", "model_assignment": "qwen-builder"}
    if allowed:
        assert policy.for_task(project, task).primary.name == "qwen"
    else:
        with pytest.raises(ValueError, match="qwen assignments are limited"):
            policy.for_task(project, task)


def test_a_model_that_declines_the_guarded_write_proves_nothing(tmp_path):
    caps = probe.probe_functional(_ToolOnly(False, declines_guarded=True),
                                  Installation("local-opencode", sys.executable, "test"),
                                  CapabilitySet(adapter="local-opencode"), fixture_root=tmp_path)
    assert caps.has("prewrite_file_guard") is False
    assert caps.has("tool_only_confinement") is False  # both refusals must be observed


def _measured(tool_only=True):
    caps = CapabilitySet(adapter="local-opencode")
    for name in ("resume_session", "structured_output", "mcp_stdio", "event_stream", "prewrite_file_guard"):
        caps.set(name, True)
    caps.set("tool_only_confinement", tool_only)
    return {"local-opencode": caps}


@pytest.mark.parametrize("kind,measured,chosen", [
    ("SAFE_PARALLEL", True, "qwen"), ("TEST_ONLY", True, "qwen"),
    ("SAFE_PARALLEL", False, None),   # no probe evidence: still refused
    ("HOTSPOT", True, None),          # needs an OS sandbox
])
def test_worker_choice_follows_the_measured_probe(project, conn, kind, measured, chosen):
    from agentkit import models
    project.raw["model_policy"] = {"assignments": {"qwen-builder": QWEN}}
    task = {"id": 1, "kind": kind, "complexity": "easy", "model_assignment": "qwen-builder", "role": "backend-builder"}
    try:
        candidate, reason = models.choose_worker(conn, project, task, _measured(measured))
    except ValueError:  # the policy itself refuses HOTSPOT for qwen
        candidate, reason = None, "policy"
    assert (candidate.name if candidate else None) == chosen, reason
