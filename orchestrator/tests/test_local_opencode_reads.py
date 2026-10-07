"""F5: a local Qwen task that declares its inputs may read only those files.

Qwen assessments read files outside their assignment because the adapter
allowed every read, and glob/grep could reveal any file's content anyway.
"""
import json

import pytest

from agentkit.adapters.local_opencode import LocalOpenCodeAdapter

SECRETS = ("*.env", "*.env.*", "*.pem", "*.key")


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    root = tmp_path / "local-opencode"
    root.mkdir()
    data = {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:8080/v1"},
                                   "models": {"qwen3.8-27b-q8-tuber": {}}}}}
    (root / "opencode.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENTKIT_LOCAL_OPENCODE", str(root))
    return root


def _permissions(project, **task):
    adapter = LocalOpenCodeAdapter()
    adapter.detect = lambda: None
    launch = adapter.build_launch({"id": 1, "kind": "RESEARCH", "complexity": "easy", "model_profile": "qwen",
                                   **task}, project.root, "researcher", project, prompt="Assess")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert config["agent"]["agentkit-worker"]["permission"] == config["permission"]
    assert json.loads(launch.env["OPENCODE_PERMISSION"]) == config["permission"]
    return config["permission"]


@pytest.mark.parametrize("declared", [["AGENTS.md", "backend/run_state.py"],
                                      json.dumps(["AGENTS.md", "backend/run_state.py"])])
def test_declared_inputs_are_the_only_readable_files(project, local_source, declared):
    permission = _permissions(project, expected_read=declared)
    read = permission["read"]
    assert read["*"] == "deny"
    for path in ("AGENTS.md", "backend/run_state.py", "backend\\run_state.py"):
        assert read["*" + path] == "allow"
    for pattern in SECRETS:
        assert read[pattern] == "deny"
    assert next(iter(read)) == "*" and list(read)[-len(SECRETS):] == list(SECRETS)
    for tool in ("glob", "grep", "list", "external_directory"):
        assert permission[tool] == "deny"
    assert permission["*"] == "deny" and permission["agentkit_brief"] == "allow"


def test_a_task_may_also_read_its_own_declared_outputs(project, local_source):
    read = _permissions(project, expected_read=["AGENTS.md"], expected_write=[".ai/notes/finding.md"])["read"]
    assert read["*.ai/notes/finding.md"] == "allow" and read["*AGENTS.md"] == "allow"


def test_a_task_without_declared_inputs_keeps_the_previous_rule(project, local_source):
    permission = _permissions(project)
    assert permission["read"]["*"] == "allow"
    for pattern in SECRETS:
        assert permission["read"][pattern] == "deny"
    assert permission["glob"] == "allow" and permission["grep"] == "allow"
