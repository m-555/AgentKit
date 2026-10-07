"""An operator lease lives by its TTL, not by a worker process.

The operator task has no worker run by design. Reconcile used to treat it as a
lost worker on every supervisor pass: it marked the task STALE and released the
lease seconds after it was granted, and the stale claim then refused the
operator's own edits.
"""
from agentkit import db, operator, reconcile
from agentkit.leases import decide

PATH = "services/media.py"


def _operator_task(conn):
    return db.get_task_by_spec(conn, operator.OPERATOR_SPEC_ID)


def test_reconcile_keeps_a_held_operator_lease(project_root, conn, project):
    assert operator.acquire(conn, [PATH], reason="manual edit").ok
    reconcile.reconcile(project_root)
    task = _operator_task(conn)
    assert task["status"] == "RUNNING"
    assert any(int(lease["task_id"]) == task["id"] for lease in db.active_leases(conn))
    assert decide(conn, project, PATH, None).allowed


def test_a_stale_operator_claim_does_not_block_the_operator(project_root, conn, project):
    assert operator.acquire(conn, [PATH], reason="manual edit").ok
    task = _operator_task(conn)
    db.release_leases(conn, task["id"], reason="lease expired")
    db.set_status(conn, task["id"], "STALE", actor="scheduler", cause="lease expired")
    verdict = decide(conn, project, PATH, None)
    assert not verdict.allowed
    assert verdict.code == "unmanaged_session"
    assert "agentkit operator acquire" in verdict.reason
    assert operator.acquire(conn, [PATH], reason="manual edit again").ok
    assert decide(conn, project, PATH, None).allowed


def test_a_running_worker_still_blocks_an_untasked_session(project_root, conn, project, make_task):
    make_task("owns media", [PATH])
    verdict = decide(conn, project, PATH, None)
    assert not verdict.allowed and verdict.code == "unmanaged_session_blocked"
