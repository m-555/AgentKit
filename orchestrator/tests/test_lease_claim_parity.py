"""F37: a lease grant refuses every claim the edit-time check would enforce.

On 2026-10-11 a pilot project's local Qwen trial (task 171) was granted an exclusive
lease on main.py while superseded task 153, in NEEDS_REPLAN, still owned it.
The grant only counted leases of active tasks; the edit-time check also counts
the owned paths of NEEDS_REPLAN, STALE, FAILED and BLOCKED tasks. So the worker
launched, edited, and the host audit blocked it three minutes later. The grant
now uses the same claim set, as `leases.conflicts` already promised.
"""
from __future__ import annotations

import pytest

from agentkit import db, leases


@pytest.mark.parametrize("status", ["READY", "BLOCKED", "STALE", "NEEDS_REPLAN", "FAILED"])
def test_a_path_another_open_task_owns_is_never_granted(conn, make_task, status):
    make_task("old attempt", ["main.py"], kind="HOTSPOT", status=status)
    trial = make_task("trial", ["main.py"], kind="HOTSPOT", status="READY")
    assert leases.conflicts(conn, ["main.py"], trial)
    granted, clashes = db.try_acquire_leases(conn, trial, ["main.py"])
    assert granted == [] and clashes[0]["requested"] == "main.py"


@pytest.mark.parametrize("status", ["PLANNED", "DONE", "CANCELLED"])
def test_planned_or_closed_owners_do_not_block(conn, make_task, status):
    make_task("other", ["main.py"], kind="HOTSPOT", status=status)
    trial = make_task("trial", ["main.py"], kind="HOTSPOT", status="READY")
    assert not leases.conflicts(conn, ["main.py"], trial)
    assert db.try_acquire_leases(conn, trial, ["main.py"]) == (["main.py"], [])

