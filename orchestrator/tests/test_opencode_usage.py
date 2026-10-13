"""OpenCode public step counters are counted once without reading reasoning text."""
import json

import pytest

from agentkit import session_usage


def step(identifier="step-1", reason="stop", **overrides):
    tokens = {"input": 120, "output": 50, "reasoning": 7,
              "cache": {"read": 30, "write": 10}}
    tokens.update(overrides)
    return {"type": "step_finish", "sessionID": "session-a",
            "part": {"id": identifier, "reason": reason, "tokens": tokens}}


def observe(tmp_path, events, wrapped=False):
    path = tmp_path / ".ai/runtime/process-17/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"channel": "stdout", "text": json.dumps(e)} if wrapped else e for e in events]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return session_usage.read_usage(tmp_path, 17)


@pytest.mark.parametrize("wrapped", [False, True])
def test_reports_explicit_counters_and_deduplicates_steps(tmp_path, wrapped):
    result = observe(tmp_path, [step(reason="tool-calls"), step(reason="tool-calls"),
                               step("step-2", input=10, output=5, reasoning=0)], wrapped)
    assert result["input_tokens"] == 130
    assert result["output_tokens"] == 55
    assert result["thinking_tokens"] == 7
    assert result["cached_input_tokens"] == 60
    assert result["cache_write_input_tokens"] == 20
    assert result["source"] == "opencode_steps"
    assert result["complete"]


def test_tool_step_is_not_terminal_and_new_step_clears_completion(tmp_path):
    assert not observe(tmp_path, [step(reason="tool-calls")])["complete"]
    assert not observe(tmp_path, [step(), {"type": "step_start"}])["complete"]


def test_missing_reasoning_stays_unknown_and_zero_is_measured(tmp_path):
    event = step(reasoning=0)
    assert observe(tmp_path, [event])["thinking_tokens"] == 0
    del event["part"]["tokens"]["reasoning"]
    assert observe(tmp_path, [event])["thinking_tokens"] is None


@pytest.mark.parametrize("counter", [True, -1, "50"])
def test_invalid_counters_do_not_become_measured_usage(tmp_path, counter):
    result = observe(tmp_path, [step(output=counter)])
    assert result["output_tokens"] is None
    assert not result["complete"]


def test_anonymous_steps_are_explicitly_partial(tmp_path):
    result = observe(tmp_path, [step(identifier=None)])
    assert result["source"].endswith(":partial")
    assert not result["complete"]
