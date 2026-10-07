"""Exact committed tests can await a correction without another paid worker."""
import pytest
from test_test_refresh import prepare

from agentkit import db, host_completion, repo, test_hold, test_refresh


def setup_hold(conn, project_root, project, monkeypatch):
    identifier, source, work, old = prepare(conn, project_root, project, monkeypatch)
    db.update_task(conn, identifier, status="READY", depends_on=["source"])
    return identifier, source, work, old


def test_hold_then_refresh_preserves_committed_test_bytes(conn, project_root, project, monkeypatch):
    identifier, source, work, old = setup_hold(conn, project_root, project, monkeypatch)
    original = (work / "tests/carried.py").read_bytes()
    result = test_hold.hold(conn, project, identifier, source, "Independent test needs accepted correction")
    assert result["held"] and result["head"] == old
    assert db.get_task(conn, identifier)["status"] == "BLOCKED"
    assert repo.head_commit(work) == old
    refreshed = test_refresh.refresh(conn, project, identifier, source)
    assert refreshed["passed"] and db.get_task(conn, identifier)["status"] == "REVIEW"
    assert (work / "tests/carried.py").read_bytes() == original


def test_live_owner_prevents_hold(conn, project_root, project, monkeypatch):
    identifier, source, work, old = setup_hold(conn, project_root, project, monkeypatch)
    def refuse(*args):
        raise PermissionError("previous monitor or child may still own this task")
    monkeypatch.setattr(host_completion, "authority", refuse)
    with pytest.raises(PermissionError, match="own"):
        test_hold.hold(conn, project, identifier, source, "Do not race an owner")
    assert db.get_task(conn, identifier)["status"] == "READY"
    assert repo.head_commit(work) == old


def test_dirty_commit_is_not_held_or_rewritten(conn, project_root, project, monkeypatch):
    identifier, source, work, old = setup_hold(conn, project_root, project, monkeypatch)
    (work / "tests/carried.py").write_text("dirty draft")
    with pytest.raises(ValueError, match="clean"):
        test_hold.hold(conn, project, identifier, source, "Preserve dirty draft")
    assert db.get_task(conn, identifier)["status"] == "READY"
    assert repo.head_commit(work) == old and (work / "tests/carried.py").read_text() == "dirty draft"


@pytest.mark.parametrize("field,value", [("depends_on", []), ("job_id", "another-job"), ("status", "CANCELLED")])
def test_unrelated_or_cancelled_source_is_refused(conn, project_root, project, monkeypatch, field, value):
    identifier, source, work, old = setup_hold(conn, project_root, project, monkeypatch)
    db.update_task(conn, identifier if field == "depends_on" else source, **{field: value})
    with pytest.raises(ValueError, match="declared same-job"):
        test_hold.hold(conn, project, identifier, source, "Reject unrelated correction")
    assert db.get_task(conn, identifier)["status"] == "READY" and repo.head_commit(work) == old
