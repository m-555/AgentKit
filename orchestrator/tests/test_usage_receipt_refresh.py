"""Counter parser upgrades refresh old receipts without losing retired logs."""
import json

from agentkit import usage_receipts


def test_old_receipt_is_refreshed_even_when_event_fingerprint_is_unchanged(tmp_path):
    source = tmp_path / ".ai/runtime/process-17/events.jsonl"
    source.parent.mkdir(parents=True)
    event = {"type": "step_finish", "sessionID": "session", "part": {
        "id": "step", "reason": "stop", "tokens": {"input": 10, "output": 4, "reasoning": 0}}}
    source.write_text(json.dumps(event) + "\n", encoding="utf-8")
    receipt = {"version": 1, "process_id": 17, "fingerprint": usage_receipts.fingerprint(source),
               "usage": {"input_tokens": None, "output_tokens": None, "source": "unavailable"}}
    source.with_name("usage.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert usage_receipts.capture(tmp_path, 17)
    assert usage_receipts.read(tmp_path, 17)["usage"]["input_tokens"] == 10
    assert not usage_receipts.capture(tmp_path, 17)


def test_older_receipt_remains_readable_when_events_are_retired(tmp_path):
    path = tmp_path / ".ai/runtime/process-17/usage.json"
    path.parent.mkdir(parents=True)
    receipt = {"version": 1, "process_id": 17,
               "usage": {"input_tokens": 10, "output_tokens": 4, "source": "codex_turns"}}
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert not usage_receipts.capture(tmp_path, 17)
    assert usage_receipts.read(tmp_path, 17)["usage"]["input_tokens"] == 10
