"""Provider-free counter fixtures verify exact usage without transcript disclosure."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit import session_usage

FIELDS = (
    "input_tokens", "output_tokens", "thinking_tokens", "cached_input_tokens",
    "cache_write_input_tokens",
)
PROCESS = 17


def codex_usage(input_tokens=120, output_tokens=50, **extra):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_input_tokens": 30, **extra}


def claude_usage(input_tokens=120, output_tokens=50, **extra):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10, **extra}


def turn(identifier, usage):
    event = {"type": "turn.completed", "usage": usage}
    if identifier is not None:
        event["turn_id"] = identifier
    return event


def assistant(identifier, usage):
    return {"type": "assistant", "message": {"id": identifier, "usage": usage,
            "content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"}]}}


def result(usage):
    return {"type": "result", "usage": usage, "result": "PRIVATE_OUTPUT"}


def line(event, *, wrapped=False, channel="stdout"):
    record = {"channel": channel, "text": json.dumps(event)} if wrapped else event
    return (json.dumps(record) + "\n").encode("utf-8")


def stream(root, events, *, wrapped=False):
    path = root / ".ai" / "runtime" / f"process-{PROCESS}" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line(event, wrapped=wrapped) for event in events))
    return path


def read(root):
    observed = session_usage.read_usage(root, PROCESS)
    assert set(observed) == {*FIELDS, "source", "complete"}
    assert isinstance(observed["source"], str) and observed["source"]
    assert type(observed["complete"]) is bool
    assert all(value is None or (type(value) is int and value >= 0)
               for value in (observed[field] for field in FIELDS))
    return observed


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_public_usage_events_preserve_provider_reported_counters(tmp_path, wrapped, provider):
    if provider == "claude":
        event = result(claude_usage(thinking_tokens=7))
    else:
        event = turn("turn-1", codex_usage(cache_write_input_tokens=10,
                                          output_tokens_details={"reasoning_tokens": 7}))
    stream(tmp_path, [event], wrapped=wrapped)
    observed = read(tmp_path)
    assert {field: observed[field] for field in FIELDS} == dict(zip(FIELDS, [120, 50, 7, 30, 10], strict=True))
    assert observed["complete"]
    assert "PRIVATE" not in json.dumps(observed)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_missing_thinking_is_unknown_even_when_reasoning_text_exists(tmp_path, provider):
    if provider == "claude":
        events = [assistant("message-1", claude_usage()), result(claude_usage())]
    else:
        events = [{"type": "item.completed", "item": {"type": "reasoning",
                   "text": "PRIVATE_REASONING"}}, turn("turn-1", codex_usage())]
    stream(tmp_path, events)
    observed = read(tmp_path)
    assert observed["thinking_tokens"] is None
    assert observed["input_tokens"] == 120 and observed["output_tokens"] == 50
    assert observed["complete"]


@pytest.mark.parametrize("provider,detail", [
    ("codex", {"thinking_tokens": 9}),
    ("codex", {"reasoning_tokens": 9}),
    ("codex", {"reasoning_output_tokens": 9}),
    ("codex", {"output_tokens_details": {"reasoning_tokens": 9}}),
    ("claude", {"output_tokens_details": {"thinking_tokens": 9}}),
])
def test_thinking_is_only_read_from_explicit_numeric_provider_fields(tmp_path, provider, detail):
    event = (result(claude_usage(**detail)) if provider == "claude" else
             turn("turn-1", codex_usage(**detail)))
    stream(tmp_path, [event])
    assert read(tmp_path)["thinking_tokens"] == 9


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_omitted_cache_counters_remain_unknown(tmp_path, provider):
    usage = {"input_tokens": 8, "output_tokens": 3}
    event = result(usage) if provider == "claude" else turn("turn-1", usage)
    stream(tmp_path, [event])
    observed = read(tmp_path)
    assert observed["input_tokens"] == 8
    assert observed["cached_input_tokens"] is None
    assert observed["cache_write_input_tokens"] is None
    assert observed["thinking_tokens"] is None
    assert observed["complete"]


def test_claude_cumulative_message_updates_do_not_double_count(tmp_path):
    events = [
        assistant("message-1", claude_usage(10, 3, thinking_tokens=2)),
        assistant("message-1", claude_usage(10, 8, thinking_tokens=4)),
        assistant("message-1", claude_usage(10, 8, thinking_tokens=4)),
        assistant("message-2", claude_usage(7, 4, thinking_tokens=2)),
    ]
    stream(tmp_path, events, wrapped=True)
    observed = read(tmp_path)
    assert [observed[field] for field in FIELDS] == [17, 12, 6, 60, 20]
    assert not observed["complete"]  # Assistant usage alone is not a terminal summary.


def test_claude_final_summary_supersedes_messages_and_duplicate_results(tmp_path):
    final = result(claude_usage(99, 44, thinking_tokens=6))
    stream(tmp_path, [assistant("message-1", claude_usage(10, 5)), final, final])
    observed = read(tmp_path)
    assert [observed[field] for field in FIELDS] == [99, 44, 6, 30, 10]
    assert observed["complete"]


def test_codex_duplicate_turn_ids_are_not_summed_twice(tmp_path):
    first = turn("turn-1", codex_usage(100, 20))
    stream(tmp_path, [first, first, turn("turn-2", codex_usage(50, 5, cached_input_tokens=10))])
    observed = read(tmp_path)
    assert observed["input_tokens"] == 150 and observed["output_tokens"] == 25
    assert observed["cached_input_tokens"] == 40
    assert observed["thinking_tokens"] is None and observed["complete"]
    assert read(tmp_path) == observed  # Repeated reads cannot recount the same file.


def test_identical_codex_turns_are_distinct_after_turn_started(tmp_path):
    usage = codex_usage(10, 5, cached_input_tokens=2, thinking_tokens=0,
                        cache_write_input_tokens=0)
    stream(tmp_path, [{"type": "turn.started"}, turn(None, usage),
                      {"type": "turn.started"}, turn(None, usage)])
    observed = read(tmp_path)
    assert [observed[field] for field in FIELDS] == [20, 10, 0, 4, 0]
    assert observed["complete"]


def test_stderr_usage_is_never_counted(tmp_path):
    path = stream(tmp_path, [turn("real", codex_usage(10, 4))], wrapped=True)
    with path.open("ab") as writer:
        writer.write(line(turn("stderr", codex_usage(900, 900)), wrapped=True, channel="stderr"))
    observed = read(tmp_path)
    assert observed["input_tokens"] == 10 and observed["output_tokens"] == 4


def test_appended_partial_record_is_counted_only_after_newline(tmp_path):
    path = stream(tmp_path, [turn("turn-1", codex_usage(10, 4))])
    assert read(tmp_path)["complete"]
    pending = line(turn("turn-2", codex_usage(20, 6)))
    with path.open("ab") as writer:
        writer.write(pending[:-1])
    observed = read(tmp_path)
    assert observed["input_tokens"] == 10 and not observed["complete"]
    with path.open("ab") as writer:
        writer.write(b"\n")
    observed = read(tmp_path)
    assert observed["input_tokens"] == 30 and observed["output_tokens"] == 10
    assert observed["complete"]
    assert read(tmp_path) == observed


def test_truncated_log_resets_previously_observed_totals(tmp_path):
    path = stream(tmp_path, [turn("old-long-identifier", codex_usage(100, 20))])
    assert read(tmp_path)["input_tokens"] == 100
    path.write_bytes(line(turn("new", codex_usage(4, 1))))
    observed = read(tmp_path)
    assert observed["input_tokens"] == 4 and observed["output_tokens"] == 1
    assert observed["complete"]


def test_same_length_file_replacement_does_not_reuse_old_counters(tmp_path):
    path = stream(tmp_path, [turn("same", codex_usage(10, 20))])
    assert read(tmp_path)["input_tokens"] == 10
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(line(turn("same", codex_usage(30, 40))))
    assert replacement.stat().st_size == path.stat().st_size
    replacement.replace(path)
    observed = read(tmp_path)
    assert observed["input_tokens"] == 30 and observed["output_tokens"] == 40


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("bad", [-1, True, False, 1.5, "4", None])
def test_invalid_required_values_never_become_invented_counters(tmp_path, field, bad):
    usage = codex_usage(9, 9)
    usage[field] = bad
    stream(tmp_path, [turn("turn-1", usage)])
    observed = read(tmp_path)
    assert observed[field] is None
    other = "output_tokens" if field == "input_tokens" else "input_tokens"
    assert observed[other] == 9
    assert not observed["complete"]


@pytest.mark.parametrize("bad", [-1, True, False, 1.5, "4"])
def test_invalid_optional_values_are_unknown_and_mark_incomplete(tmp_path, bad):
    stream(tmp_path, [turn("turn-1", codex_usage(cached_input_tokens=bad,
                          thinking_tokens=bad, cache_write_input_tokens=bad))])
    observed = read(tmp_path)
    assert observed["cached_input_tokens"] is None
    assert observed["thinking_tokens"] is None
    assert observed["cache_write_input_tokens"] is None
    assert not observed["complete"]


def test_malformed_records_do_not_erase_valid_observations(tmp_path):
    path = stream(tmp_path, [turn("turn-1", codex_usage(7, 2))])
    with path.open("ab") as writer:
        writer.write(b"{bad json\nnull\n[]\n" + b"[" * 1500 + b"]" * 1500 + b"\n")
        writer.write(line(turn("turn-2", codex_usage(3, 1))))
    observed = read(tmp_path)
    assert observed["input_tokens"] == 10 and observed["output_tokens"] == 3
    assert not observed["complete"]


def test_bounded_tail_never_claims_complete_historical_usage(tmp_path):
    path = stream(tmp_path, [])
    path.write_bytes(b'{"text":"' + b"x" * 2_097_152 + b'"}\n' +
                     line(turn("last", codex_usage(9, 2))))
    observed = read(tmp_path)
    assert observed["input_tokens"] == 9
    assert not observed["complete"]


def test_missing_stream_does_not_create_runtime_state(tmp_path):
    root = tmp_path / "absent-project"
    observed = read(root)
    assert all(observed[field] is None for field in FIELDS)
    assert not observed["complete"] and not root.exists()


def test_resolved_stream_outside_project_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "project"
    path = stream(root, [turn("private", codex_usage(1234, 5678))])
    outside = tmp_path / "outside-events.jsonl"
    outside.write_bytes(path.read_bytes())
    original = Path.resolve

    def escaped(candidate, *args, **kwargs):
        return outside if candidate == path else original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped)
    observed = read(root)
    assert all(observed[field] is None for field in FIELDS)
    assert not observed["complete"]


@pytest.mark.parametrize("identifier", [-1, True, "../../outside"])
def test_invalid_process_identifiers_cannot_read_streams(tmp_path, identifier):
    stream(tmp_path, [turn("real", codex_usage())])
    observed = session_usage.read_usage(tmp_path, identifier)
    assert all(observed[field] is None for field in FIELDS)
    assert not observed["complete"]
