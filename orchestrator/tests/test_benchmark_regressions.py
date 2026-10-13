"""Universal regressions discovered by an isolated project benchmark."""
import json
import pytest
from agentkit.codex_mcp_policy import approval_flags
from agentkit import project_usage
from agentkit.probe_evidence import sandbox_denial


def settings(role):
    flags = approval_flags(role, readonly=True)
    return dict(item.split("=", 1) for item in flags[1::2])


def test_coordinator_can_plan_its_authenticated_job_noninteractively():
    values = settings("coordinator")
    for tool in ("job_decision", "task_define", "job_plan_ready", "job_accept",
                 "task_requeue", "integration_retry", "project_configure",
                 "manager_audit", "manager_ack", "manager_messages"):
        assert values[f"mcp_servers.agentkit.tools.{tool}.approval_mode"] == '"approve"'
    assert values["mcp_servers.agentkit.default_tools_approval_mode"] == '"prompt"'
    for tool in ("job_create", "job_start", "task_commit", "lease_request"):
        assert f"mcp_servers.agentkit.tools.{tool}.approval_mode" not in values


@pytest.mark.parametrize("role", ["reviewer", "researcher", "architect"])
def test_other_readonly_roles_do_not_get_coordinator_mutation_approval(role):
    for tool in ("task_define", "job_accept", "task_requeue", "project_configure"):
        assert f"mcp_servers.agentkit.tools.{tool}.approval_mode" not in settings(role)


@pytest.mark.parametrize("reported,expected", [
    ("C:/work/very-long/fixt\nure-outside.txt", True),
    ("C:/work/very-long/fixture-outside.txt.backup", False),
    ("C:/work/other/fixt\nure-outside.txt", False),
])
def test_wrapped_windows_access_denied_path_still_requires_exact_target(reported, expected):
    target = "C:/work/very-long/fixture-outside.txt"
    event = {"type": "item.completed", "item": {"type": "command_execution",
        "command": f'echo BREACHED > "{target}"', "exit_code": 1,
        "aggregated_output": f"out-file : Access to the path '{reported}' is denied.\nUnauthorizedAccessException"}}
    assert sandbox_denial(json.dumps(event), target) is expected


def test_unquoted_wrapped_path_does_not_certify_boundary():
    target = "C:/work/very-long/fixture-outside.txt"
    event = {"type": "item.completed", "item": {"type": "command_execution",
        "command": f'echo BREACHED > "{target}"', "exit_code": 1,
        "aggregated_output": "Access is denied: C:/work/very-long/fixt\nure-outside.txt"}}
    assert not sandbox_denial(json.dumps(event), target)


def test_local_project_totals_include_cache_once_and_reasoning_as_subset():
    usage = {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0,
             "cached_input_tokens": 100, "cache_write_input_tokens": 3, "complete": True}
    bucket = project_usage._empty()
    project_usage._add(bucket, usage, "local-opencode")
    assert bucket["normalized_input_tokens"] == 113
    assert bucket["recorded_tokens"] == 118
    assert bucket["complete_launches"] == 1
    assert bucket["counters"]["thinking_tokens"] == 0
    # A local provider has no cloud cache-price discount; unit token weight only.
    assert bucket["cost_weighted_input_tokens"] == 113


def test_local_missing_cache_counters_remain_partial():
    usage = {"input_tokens": 10, "output_tokens": 5, "complete": True}
    bucket = project_usage._empty()
    project_usage._add(bucket, usage, "local-opencode")
    assert bucket["recorded_tokens"] == 15
    assert bucket["complete_launches"] == 0
    assert bucket["partial_launches"] == 1
    assert bucket["counters"]["cached_input_tokens"] is None
