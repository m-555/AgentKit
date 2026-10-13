"""A model assignment can be limited to task roles.

After Qwen's runs on a test file and a UI component, the user decided the local
Qwen builder writes backend code only. `roles` on an assignment makes that a
rule the policy enforces for every planner, including the automatic manager.
"""
from __future__ import annotations

import pytest

from agentkit import policy
from agentkit.config import ProjectConfig

RAW = {"model_policy": {"assignments": {"qwen-builder": {
    "profile": "qwen", "model": "local/qwen3.8-27b", "roles": ["backend-builder"]}}}}


def _task(role, kind="DEPENDENT"):
    return {"kind": kind, "role": role, "complexity": "standard", "model_assignment": "qwen-builder"}


def test_the_allowed_role_is_served(tmp_path):
    result = policy.for_task(ProjectConfig(root=tmp_path, raw=RAW), _task("backend-builder"))
    assert result.primary.name == "qwen" and result.roles == ("backend-builder",)


@pytest.mark.parametrize("role,kind", [("backend-tester", "TEST_ONLY"), ("frontend-builder", "DEPENDENT"),
                                       ("frontend-tester", "TEST_ONLY")])
def test_other_roles_are_refused(tmp_path, role, kind):
    with pytest.raises(ValueError, match="serves only backend-builder"):
        policy.for_task(ProjectConfig(root=tmp_path, raw=RAW), _task(role, kind))


def test_an_assignment_without_roles_serves_any_role(tmp_path):
    raw = {"model_policy": {"assignments": {"qwen-builder": {"profile": "qwen", "model": "local/qwen3.8-27b"}}}}
    assert policy.for_task(ProjectConfig(root=tmp_path, raw=raw), _task("frontend-tester", "TEST_ONLY"))


def test_roles_must_be_a_list_of_names(tmp_path):
    raw = {"model_policy": {"assignments": {"qwen-builder": {
        "profile": "qwen", "model": "local/qwen3.8-27b", "roles": "backend-builder"}}}}
    with pytest.raises(ValueError, match="roles must be a list"):
        policy.for_task(ProjectConfig(root=tmp_path, raw=raw), _task("backend-builder"))


def test_assignment_metadata_exposes_enforced_role_restriction(tmp_path):
    assignment = policy.named(ProjectConfig(root=tmp_path, raw=RAW), 'qwen-builder')
    payload = assignment.to_dict()
    assert payload['roles'] == ['backend-builder']
    assert payload['primary'] == assignment.primary.to_dict()
    assert payload['fallback'] == []
