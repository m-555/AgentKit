"""Qwen takes normal tasks, not only easy ones (2026-10-10).

The user asked to try the local Qwen builder on a normal backend task and to
remove the rule that limited it to easy tasks. The spec, the model policy, the
worker chooser and the OpenCode adapter all accept a standard or complex task
for Qwen now. Its kind limits stay: tool-layer confinement is not an OS sandbox,
so hot-path, contract and decoupling work still needs a sandboxed worker.
Sonnet keeps its easy-only rule.
"""
from __future__ import annotations

import pytest

from agentkit import models, policy, spec
from agentkit.config import ProjectConfig

QWEN = {"model_policy": {"assignments": {"qwen-builder": {"profile": "qwen", "model": "local/qwen3.8-27b"}}}}


@pytest.mark.parametrize("complexity", ["easy", "standard", "complex"])
def test_qwen_takes_any_complexity_of_a_bounded_kind(tmp_path, complexity):
    task = {"kind": "DEPENDENT", "complexity": complexity, "model_assignment": "qwen-builder"}
    assert models.small_model_fits("qwen", task)
    spec.TaskSpec.from_dict({"id": "build", "title": "Build", "kind": "DEPENDENT", "complexity": complexity,
                             "model_profile": "qwen", "expected_paths": {"write": ["a.py"]}})
    assert policy.for_task(ProjectConfig(root=tmp_path, raw=QWEN), task).primary.name == "qwen"


@pytest.mark.parametrize("kind", ["HOTSPOT", "CONTRACT_CHANGE", "DECOUPLE"])
def test_qwen_still_never_takes_a_high_risk_kind(tmp_path, kind):
    with pytest.raises(ValueError, match="high-risk"):
        policy.for_task(ProjectConfig(root=tmp_path, raw=QWEN),
                        {"kind": kind, "complexity": "easy", "model_assignment": "qwen-builder"})


def test_sonnet_keeps_its_easy_only_rule():
    assert not models.small_model_fits("sonnet", {"kind": "SAFE_PARALLEL", "complexity": "standard"})
    assert models.small_model_fits("sonnet", {"kind": "SAFE_PARALLEL", "complexity": "easy"})
