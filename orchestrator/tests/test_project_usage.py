"""Project totals count historical launches once, with honest provider coverage."""
from __future__ import annotations

import json

from agentkit import db, project_usage, session_usage, usage_receipts


def launch(conn, provider="codex", status="FINISHED", purpose="worker", task_id=None):
    identifier = conn.execute(
        "INSERT INTO processes(purpose,provider,status,started_at,launch_json,task_id) VALUES(?,?,?,?,?,?)",
        (purpose, provider, status, db.utcnow(), "{}", task_id)).lastrowid
    conn.commit()
    return identifier


def log(root, identifier, events):
    path = root / ".ai" / "runtime" / f"process-{identifier}" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def codex(input_tokens=100, output_tokens=20):
    return {"type": "turn.completed", "turn_id": "one", "usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_input_tokens": min(input_tokens, 70),
        "thinking_tokens": 8}}


def claude():
    return {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 70, "cache_creation_input_tokens": 5, "thinking_tokens": 8}}


def test_normalized_totals_do_not_double_add_cache_or_thinking(project_root, conn):
    task = db.create_task(conn, title="source", role="backend-builder")
    first = launch(conn, task_id=task)
    second = launch(conn, provider="claude-code", status="FAILED", purpose="review")
    log(project_root, first, [codex(), codex()])
    log(project_root, second, [claude(), claude()])
    assert usage_receipts.capture_stopped(project_root, conn) == 2
    before = list(conn.iterdump())
    snapshot = project_usage.snapshot(project_root)
    assert snapshot["recorded_tokens"] == 225  # 100+20 + 10+70+5+20
    assert snapshot["normalized_input_tokens"] == 185
    assert snapshot["counters"]["thinking_tokens"] == 16  # Included, not added again.
    assert snapshot["complete"]
    assert snapshot["complete_launches"] == 2
    assert {row["role"] for row in snapshot["roles"]} == {"backend-builder", "review"}
    assert usage_receipts.capture_stopped(project_root, conn) == 0
    assert project_usage.snapshot(project_root)["recorded_tokens"] == 225
    assert list(conn.iterdump()) == before


def test_all_launches_are_counted_beyond_dashboard_pagination(project_root, conn):
    for _ in range(205):
        identifier = launch(conn)
        log(project_root, identifier, [codex(1, 2)])
        usage_receipts.capture(project_root, identifier)
    result = project_usage.snapshot(project_root)
    assert result["launches"] == 205
    assert result["recorded_tokens"] == 615


def test_unknown_missing_and_external_usage_is_not_zero(project_root, conn):
    launch(conn)
    partial = launch(conn, provider="claude-code")
    log(project_root, partial, [{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}])
    usage_receipts.capture(project_root, partial)
    conn.execute("INSERT INTO manager_leases(job_id,holder,provider,model,effort,session_ref,pid,"
                 "ttl_seconds,takeover_grace,token_hash,acquired_at,heartbeat_at) "
                 "VALUES('job','chat','codex','sol','high','external',1,90,30,'hash',?,?)",
                 (db.utcnow(), db.utcnow()))
    conn.commit()
    result = project_usage.snapshot(project_root)
    assert result["recorded_tokens"] == 15
    assert not result["complete"]
    assert result["missing_launches"] == 1 and result["partial_launches"] == 1
    assert result["external_sessions_unmetered"] == 1
    assert result["counters"]["thinking_tokens"] is None


def test_receipts_survive_log_removal_and_reader_cache_restart(project_root, conn):
    identifier = launch(conn)
    path = log(project_root, identifier, [codex()])
    usage_receipts.capture(project_root, identifier)
    path.unlink()
    session_usage._CACHE.clear()
    assert project_usage.snapshot(project_root)["recorded_tokens"] == 120


def test_full_history_scan_counts_early_codex_turns_outside_live_tail(project_root, conn):
    identifier = launch(conn)
    events = [codex(10, 5), {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE" * 8000}},
              {"type": "turn.started", "turn_id": "two"},
              {"type": "turn.completed", "turn_id": "two", "usage": {"input_tokens": 20, "output_tokens": 7}}]
    path = log(project_root, identifier, events)
    with path.open("r+b") as stream:
        data = stream.read()
        # More than the live reader's 1 MiB limit, with early and late counters.
        filler = (json.dumps(events[1]) + "\n").encode() * 30
        stream.seek(0)
        stream.write((json.dumps(events[0]) + "\n").encode() + filler +
                     b"".join((json.dumps(event) + "\n").encode() for event in events[2:]))
        stream.truncate()
    assert data
    assert path.stat().st_size > session_usage.MAX_READ_BYTES
    assert session_usage.read_details(project_root, identifier)["input_tokens"] == 20
    assert usage_receipts.capture(project_root, identifier)
    result = project_usage.snapshot(project_root)
    assert result["recorded_tokens"] == 42 and result["complete"]
    assert "PRIVATE" not in json.dumps(usage_receipts.read(project_root, identifier))


def test_live_counters_replace_receipt_and_update_without_accumulation(project_root, conn):
    identifier = launch(conn, status="RUNNING")
    log(project_root, identifier, [codex(10, 4)])
    usage_receipts.capture(project_root, identifier)
    usage = {"input_tokens": 30, "output_tokens": 10, "complete": False}
    first = project_usage.snapshot(project_root, {identifier: usage})
    second = project_usage.snapshot(project_root, {identifier: usage})
    assert first["recorded_tokens"] == second["recorded_tokens"] == 40
    assert first["partial_launches"] == 1


def test_escaping_usage_receipt_and_invalid_numbers_are_ignored(project_root, conn, tmp_path):
    identifier = launch(conn)
    path = log(project_root, identifier, [codex()]).with_name("usage.json")
    path.write_text(json.dumps({"version": 1, "process_id": identifier,
                               "usage": {"input_tokens": True}}))
    assert usage_receipts.read(project_root, identifier) is None
    outside = tmp_path / "outside.json"
    outside.write_text('{"PRIVATE":"value"}')
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        return  # Windows installations may disallow creating symlinks.
    assert usage_receipts.read(project_root, identifier) is None
    assert not usage_receipts.capture(project_root, identifier)


def test_stopped_log_appends_replace_old_receipt_instead_of_showing_stale_total(project_root, conn):
    identifier = launch(conn)
    path = log(project_root, identifier, [codex(10, 4)])
    usage_receipts.capture(project_root, identifier)
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "turn.completed", "turn_id": "two",
                                "usage": {"input_tokens": 30, "output_tokens": 10}}) + "\n")
    assert project_usage.snapshot(project_root)["recorded_tokens"] == 54
    usage_receipts.capture(project_root, identifier)
    assert project_usage.snapshot(project_root)["recorded_tokens"] == 54
