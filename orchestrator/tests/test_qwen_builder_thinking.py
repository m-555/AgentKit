"""F33: local Qwen builders get room to write after they think.

Twice in a row a standard Qwen task (a 260-line router, then a 300-line test
file) read its inputs and then spent one nine-minute response that ended at
exactly the 24,576-token output cap with no tool call: thinking is counted as
output, so the whole budget went to thinking and nothing was written. The
user's choice: keep thinking but lower it, and raise the output. The router now
caps thinking (llama-server --reasoning-budget, 8192 tokens), and writer launches
get a 48k output budget; read-only research keeps the configured default.
"""
from __future__ import annotations

from agentkit.adapters import local_opencode
from tests.test_local_opencode_builders import _config, local_source  # noqa: F401  (fixture)


def _model(config):
    return config["provider"]["local"]["models"]["qwen3.8-27b-q8-tuber"]


def test_builders_get_a_larger_output_budget_and_keep_thinking(project, local_source):  # noqa: F811
    for kind in ("SAFE_PARALLEL", "DEPENDENT", "TEST_ONLY"):
        config, _ = _config(project, kind=kind, complexity="standard")
        model = _model(config)
        assert model["limit"]["output"] == local_opencode.WRITER_OUTPUT == 49152
        assert "chat_template_kwargs" not in (model.get("options") or {})  # thinking stays with the router


def test_research_keeps_the_configured_output(project, local_source):  # noqa: F811
    config, _ = _config(project, kind="RESEARCH", expected_write=[])
    assert "output" not in (_model(config).get("limit") or {})


def test_the_shared_provider_config_is_not_mutated(project, local_source):  # noqa: F811
    _config(project, kind="DEPENDENT")
    provider = local_opencode.local_config()
    assert "limit" not in provider["models"]["qwen3.8-27b-q8-tuber"]
