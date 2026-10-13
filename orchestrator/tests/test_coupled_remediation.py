"""F38: a coupled tester lands strict test updates together with held approved source.

On 2026-10-11 a pilot project's main.py wiring (task 171) was approved, but its combined
gate failed: existing strict tests pinned the old route set. Updating them was
a tester task that depends on 171, and remediation only accepted testers valid
before and after the change, so neither could land. A coupled hold lets that
dependent tester start from the approved source commit. Its merge brings both
through one combined gate; the held source then completes as already contained,
with no model relaunched. A branch whose base is not in integration may merge
only on that footing, so no commit outside an audited range can slip in.
"""
import pytest

from agentkit import (
    db,
    gates,
    integration_remediation,
    integration_retry,
    integrator,
    repo,
    reviews,
    scheduler,
    worktrees,
)
from tests.conftest import commit_all
from tests.test_integration_retry import failed

STRICT = "tests/test_retry_strict.py"


def _coupled(conn, project, project_root):
    source, work, head = failed(conn, project, project_root)
    db.update_task(conn, source, job_id="repair-job")
    tester = db.create_task(conn, spec_id="strict-tests", title="strict tests", role="backend-tester",
                            kind="TEST_ONLY", status="PLANNED", job_id="repair-job", depends_on=["change"],
                            expected_write=[STRICT], owned_paths=[STRICT])
    return source, work, head, tester


def _passing_gates(monkeypatch):
    monkeypatch.setattr(gates, "run_gate", lambda project, level, **kwargs: gates.GateResult(level, True))


def _tested(conn, project, project_root, tester, monkeypatch):
    work, _ = worktrees.ensure(project_root, db.get_task(conn, tester), project)
    base = repo.head_commit(work)
    (work / STRICT).write_text("def test_value():\n    assert True\n")
    head = commit_all(work, "strict tests")
    db.update_task(conn, tester, worktree=str(work), branch=repo.current_branch(work), base_sha=base)
    conn.execute("UPDATE tasks SET status='REVIEW' WHERE id=?", (tester,))
    _passing_gates(monkeypatch)
    reviews.approve(conn, project, tester, head, "PASS", "independent", "Strict tests match the wiring")
    return work, base, head


def test_a_dependent_tester_still_needs_explicit_coupling(conn, project, project_root):
    source, _, _, tester = _coupled(conn, project, project_root)
    with pytest.raises(ValueError):
        integration_retry.authorize(conn, project, source, "Strict tests change with the wiring", [tester])
    assert db.get_task(conn, source)["status"] == "FAILED"


def test_a_coupled_tester_is_ready_and_starts_from_the_approved_source(conn, project, project_root):
    source, _, head, tester = _coupled(conn, project, project_root)
    integration_retry.authorize(conn, project, source, "Strict tests change with the wiring", [tester],
                                coupled=True)
    assert db.get_task(conn, source)["status"] == "FAILED"
    assert tester in db.refresh_ready(conn)
    assert tester in [task["id"] for task in scheduler.eligible_tasks(conn)]
    work, _ = worktrees.ensure(project_root, db.get_task(conn, tester), project)
    assert repo.head_commit(work) == head


def test_both_land_through_one_gate_and_the_source_completes_without_a_model(conn, project, project_root,
                                                                           monkeypatch):
    source, _, head, tester = _coupled(conn, project, project_root)
    integration_retry.authorize(conn, project, source, "Strict tests change with the wiring", [tester],
                                coupled=True)
    db.refresh_ready(conn)
    _, base, tested = _tested(conn, project, project_root, tester, monkeypatch)
    assert base == head
    assert integrator.merge_one(conn, project, db.get_task(conn, tester)).ok
    target = integrator.ensure_integration_branch(project)
    assert repo.is_ancestor(project_root, head, target) and repo.is_ancestor(project_root, tested, target)
    assert integration_remediation.release_ready(conn, project)
    assert integrator.merge_one(conn, project, db.get_task(conn, source)).ok
    assert db.get_task(conn, source)["status"] == "DONE"
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["approval_withdrawn", "source_requeued"])
def test_a_tester_cannot_carry_a_source_that_is_no_longer_held_and_approved(conn, project, project_root,
                                                                          monkeypatch, change):
    source, _, head, tester = _coupled(conn, project, project_root)
    integration_retry.authorize(conn, project, source, "Strict tests change with the wiring", [tester],
                                coupled=True)
    db.refresh_ready(conn)
    _tested(conn, project, project_root, tester, monkeypatch)
    if change == "approval_withdrawn":
        reviews.invalidate(conn, source, head, "scope changed")
    else:
        conn.execute("UPDATE tasks SET status='READY', blocker=NULL WHERE id=?", (source,))
    outcome = integrator.merge_one(conn, project, db.get_task(conn, tester))
    assert not outcome.ok
    target = integrator.ensure_integration_branch(project)
    assert not repo.is_ancestor(project_root, head, target)


def test_the_attached_manager_holds_a_coupled_source_from_the_cli(conn, project, project_root, tmp_path, capsys):
    import os

    from agentkit import cli, jobs, manager
    source, _, head, tester = _coupled(conn, project, project_root)
    jobs.create(project_root, "repair-job", "Wire and test", "codex")
    token = manager.attach(conn, project_root, "repair-job", "root-chat", os.getpid(), session_ref="root")
    (project_root / ".ai/runtime").mkdir(exist_ok=True)
    (project_root / ".ai/runtime/manager-repair-job.credential").write_text(token)
    evidence = tmp_path / "why.txt"
    evidence.write_text("Strict tests must change with the wiring.", encoding="utf-8")
    assert cli.main(["--path", str(project_root), "manager", "integration-retry", "repair-job", "--task",
                     str(source), "--after-task", str(tester), "--coupled", "--evidence-file", str(evidence)]) == 0
    assert "coupled testers start from it" in capsys.readouterr().out
    assert integration_remediation.held_source(conn, db.get_task(conn, tester))[1] == head


def test_the_tester_packet_accepts_the_held_source_as_its_implementation(conn, project, project_root,
                                                                       monkeypatch):
    from agentkit import test_packets, workflow
    source, _, head, tester = _coupled(conn, project, project_root)
    db.update_task(conn, source, role="backend-builder")
    db.update_task(conn, tester, expected_read=["services/retry.py"])
    monkeypatch.setattr(workflow, "enabled", lambda project: True)
    with pytest.raises(ValueError, match="awaits merged implementation"):
        test_packets.verify_checkout(conn, project, db.get_task(conn, tester), project_root)
    integration_retry.authorize(conn, project, source, "Strict tests change with the wiring", [tester],
                                coupled=True)
    db.refresh_ready(conn)
    work, _ = worktrees.ensure(project_root, db.get_task(conn, tester), project)
    test_packets.verify_checkout(conn, project, db.get_task(conn, tester), work)
    assert test_packets.build(conn, project, db.get_task(conn, tester))[0]["commit"] == head
