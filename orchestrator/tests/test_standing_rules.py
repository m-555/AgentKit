"""F16: rules a worker must keep for the whole run survive context compaction.

OpenCode summarises the conversation when a local model's context fills. In a
2026-10-07 assessment the summary kept a specification's "CRLF" but dropped the
launch message's "line endings are normalized by the host", and Qwen spent six
minutes rewriting a passing file and left a syntax error. OpenCode re-sends the
agent prompt on every request and never compacts it, so the task's standing
rules belong there. Every worker also hears the host line-ending rule.
"""
import json

import pytest

from agentkit import instructions
from agentkit.adapters.local_opencode import LocalOpenCodeAdapter

NOTE = "Line endings are normalized by the host when it commits"


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    root = tmp_path / "local-opencode"
    root.mkdir()
    data = {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:8080/v1"},
                                   "models": {"qwen3.8-27b-q8-tuber": {}}}}}
    (root / "opencode.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENTKIT_LOCAL_OPENCODE", str(root))
    return root


TASK = {"id": 7, "kind": "RESEARCH", "complexity": "easy", "model_profile": "qwen",
        "title": "Assess run handoff tests", "description": "Grade the RunHandoff suite against the spec.",
        "acceptance": json.dumps(["Name each uncovered state"]),
        "expected_read": ["apps/web/RunProgress.jsx"]}


def _launch(project, **task):
    adapter = LocalOpenCodeAdapter()
    adapter.detect = lambda: None
    launch = adapter.build_launch({**TASK, **task}, project.root, "researcher", project, prompt="LAUNCH MESSAGE")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    return launch, config["agent"]["agentkit-worker"]["prompt"]


def test_the_local_system_prompt_carries_the_task_rules(project, local_source):
    launch, system = _launch(project)
    for text in ("task 7", "Assess run handoff tests", "Grade the RunHandoff suite against the spec.",
                 "Name each uncovered state", "apps/web/RunProgress.jsx", "Do not write files",
                 "agentkit_checkpoint", "task_status REVIEW"):
        assert text in system
    assert launch.stdin_text == "LAUNCH MESSAGE"


def test_the_local_system_prompt_states_the_host_line_ending_rule(project, local_source):
    assert NOTE not in _launch(project)[1]
    project.raw["source_line_endings"] = "crlf"
    assert NOTE in _launch(project)[1]


def test_an_undeclared_read_scope_is_not_described_as_confined(project, local_source):
    system = _launch(project, expected_read=[])[1]
    assert "You may read only" not in system


@pytest.mark.parametrize("task", [
    {"kind": "RESEARCH", "role": "backend-tester", "expected_write": [], "skills": []},
    {"kind": "SAFE_PARALLEL", "role": "implementer", "expected_write": ["a.py"], "skills": []},
])
def test_every_worker_hears_the_line_ending_rule_when_the_host_normalizes(project, task):
    assert NOTE not in instructions.prompt(task, project, "Call brief.")
    project.raw["source_line_endings"] = "lf"
    assert NOTE in instructions.prompt(task, project, "Call brief.")
