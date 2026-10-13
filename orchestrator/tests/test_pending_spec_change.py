"""F36: a spec change to running work stays pending until the coordinator requeues it.

On 2026-10-11 the manager moved a pilot project's main.py wiring task from Opus to the
local Qwen builder while an Opus worker had it in REVIEW. Reconcile set it to
NEEDS_REPLAN but recorded the new spec hash without the new fields, so the
requeued task ran on Opus again, and no later reconcile could notice: the hash
already matched. The change now stays pending (the old hash is kept, and it is
reported once), and a requeue applies the current spec before the task is READY.
"""
from __future__ import annotations

from agentkit import db, mcp_server, reconcile, spec
from agentkit import statemachine as sm


def _spec(expected_write=("services/alpha.py",), **changes):
    return spec.TaskSpec(spec_id="alpha", title="task alpha", expected_write=list(expected_write), **changes)


def _running(root):
    spec.save(root, [_spec(description="old")])
    reconcile.reconcile(root)
    conn = db.connect(root)
    try:
        task = db.get_task_by_spec(conn, "alpha")
        for status in (sm.READY, sm.LEASED, sm.RUNNING):
            db.set_status(conn, int(task["id"]), status, actor="scheduler")
        return int(task["id"]), task["spec_hash"]
    finally:
        conn.close()


def _changed(root):
    new = _spec(description="new", expected_write=["services/alpha.py", "services/extra.py"])
    spec.save(root, [new])
    return new


def test_a_change_to_running_work_stays_pending_and_is_reported_once(project_root):
    task_id, old_hash = _running(project_root)
    _changed(project_root)
    first, second = reconcile.reconcile(project_root), reconcile.reconcile(project_root)
    assert "alpha" in first.replan and "alpha" not in second.replan
    conn = db.connect(project_root)
    try:
        task = db.get_task(conn, task_id)
        assert task["status"] == sm.NEEDS_REPLAN and task["spec_hash"] == old_hash
        assert task["description"] == "old" and task["expected_write"] == ["services/alpha.py"]
    finally:
        conn.close()


def _requeue(root, monkeypatch, task_id):
    monkeypatch.setenv("AGENTKIT_ROOT", str(root))
    for name in ("AGENTKIT_PROCESS", "AGENTKIT_TASK"):
        monkeypatch.delenv(name, raising=False)
    assert "requeued" in mcp_server.task_requeue(task_id, "Run the current spec.")
    conn = db.connect(root)
    try:
        return db.get_task(conn, task_id)
    finally:
        conn.close()


def test_a_requeue_runs_the_current_spec(project_root, monkeypatch):
    task_id, _ = _running(project_root)
    new = _changed(project_root)
    reconcile.reconcile(project_root)
    task = _requeue(project_root, monkeypatch, task_id)
    assert task["status"] == sm.READY and task["spec_hash"] == new.hash()
    assert task["description"] == "new"
    assert task["expected_write"] == task["owned_paths"] == ["services/alpha.py", "services/extra.py"]
    assert reconcile.reconcile(project_root).updated == []


def test_a_requeue_repairs_a_row_that_recorded_the_hash_without_the_fields(project_root, monkeypatch):
    task_id, _ = _running(project_root)
    new = _changed(project_root)
    reconcile.reconcile(project_root)
    conn = db.connect(project_root)
    try:  # what reconcile wrote before F36
        db.update_task(conn, task_id, spec_hash=new.hash())
    finally:
        conn.close()
    task = _requeue(project_root, monkeypatch, task_id)
    assert task["description"] == "new" and task["owned_paths"] == ["services/alpha.py", "services/extra.py"]
