"""F21: a resolved launch refusal is not handed to the next worker as its instruction.

A refused launch records its reason as the task's blocker and, when the task had
no next action, as its next action too. A later launch that passes cleared the
blocker but kept the next action, so pilot task 75's worker read "implementation
file changed after approval ... replan tester" as its current instruction after
the manager had already replanned it, and blocked itself in 12 seconds.
A successful launch now clears a next action that only repeats a recorded
refusal; a manager's own instruction survives.
"""
from __future__ import annotations

from agentkit import db, launch_failure, models
from tests.test_empty_attempt_fresh_start import _launch, stopped_empty  # noqa: F401 - fixture

REFUSAL = "implementation file changed after approval: routes/x.py; replan tester"
OPUS = models.Profile("opus", "claude-code", "claude-opus-5-5", "xhigh", 2)


def test_a_refusal_becomes_the_next_action_only_when_none_was_set(conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    db.update_task(conn, task_id, next_action=None)
    launch_failure.record(conn, task_id, REFUSAL)
    task = db.get_task(conn, task_id)
    assert (task["blocker"], task["next_action"]) == (REFUSAL, REFUSAL)
    assert db.recent_events(conn, task_id, kind="launch_refused")[0]["detail"]["reason"] == REFUSAL


def test_a_successful_launch_clears_a_next_action_that_only_repeated_the_refusal(
        project_root, conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    db.update_task(conn, task_id, next_action=None)
    launch_failure.record(conn, task_id, REFUSAL)
    ok, detail = _launch(conn, project_root, stopped_empty, OPUS)
    assert ok, detail
    task = db.get_task(conn, task_id)
    assert task["blocker"] is None and task["next_action"] is None
    assert REFUSAL not in stopped_empty["launched"][0].stdin_text


def test_a_managers_instruction_survives_a_refusal_and_the_launch_after_it(
        project_root, conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    instruction = db.get_task(conn, task_id)["next_action"]
    launch_failure.record(conn, task_id, REFUSAL)
    assert db.get_task(conn, task_id)["next_action"] == instruction
    ok, detail = _launch(conn, project_root, stopped_empty, OPUS)
    assert ok, detail
    assert db.get_task(conn, task_id)["next_action"] == instruction


def test_a_repeated_refusal_is_logged_once(conn, stopped_empty):  # noqa: F811
    task_id = stopped_empty["task"]
    for _ in range(5):  # the supervisor retries every pass
        launch_failure.record(conn, task_id, REFUSAL)
    assert len(db.recent_events(conn, task_id, kind="launch_refused")) == 1
