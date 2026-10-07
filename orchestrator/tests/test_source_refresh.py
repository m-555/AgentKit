"""Source drafts must get accepted dependencies without model setup or lost bytes."""
import pytest
from conftest import git

from agentkit import checkpoints, db, gates, host_completion, repo, test_refresh, worktrees


def prepare(conn, project_root, project, monkeypatch):
    monkeypatch.setattr(host_completion,"authority",lambda *args: "manager")
    identifier=db.create_task(conn,title="draft",spec_id="draft",status="BLOCKED",owned_paths=["services/media.py"])
    work,_=worktrees.ensure(project_root,db.get_task(conn,identifier),project)
    base=repo.head_commit(work)
    (work/"services/media.py").write_text("VALUE = 'preserved draft'\n")
    git(work,"add","services/media.py")
    git(work,"commit","-qm","source draft")
    old=repo.head_commit(work)
    db.update_task(conn,identifier,worktree=str(work),branch=repo.current_branch(work),base_sha=base,last_commit=old,blocker="known defect needs fresh builder")
    checkpoints.write_mechanical(conn,project_root,work,identifier,"source stopped")
    branch=test_refresh.ensure_integration_branch(project)
    accepted=project_root.parent/"accepted-dependency"
    git(project_root,"worktree","add",str(accepted),branch)
    (accepted/"services/retry.py").write_text("VALUE = 'accepted dependency'\n")
    git(accepted,"add","services/retry.py")
    git(accepted,"commit","-qm","accepted dependency")
    source=db.create_task(conn,title="dependency",spec_id="dependency",status="DONE")
    db.update_task(conn,source,last_commit=repo.head_commit(accepted))
    return identifier,source,work,old,accepted


def test_refresh_keeps_builder_held_and_draft_bytes_exact(conn,project_root,project,monkeypatch):
    identifier,source,work,old,_=prepare(conn,project_root,project,monkeypatch)
    original=(work/"services/media.py").read_bytes()
    result=test_refresh.refresh_source(conn,project,identifier,source)
    assert result["passed"] and repo.is_clean(work)
    assert (work/"services/media.py").read_bytes()==original
    assert "accepted dependency" in (work/"services/retry.py").read_text()
    assert git(work,"rev-parse",result["backup"]).stdout.strip()==old
    task=db.get_task(conn,identifier)
    assert task["status"]=="BLOCKED" and task["blocker"]=="known defect needs fresh builder"
    assert db.latest_checkpoint(conn,identifier,kind="mechanical")["payload"]["head_sha"]==result["head"]
    review=conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",(identifier,)).fetchone()
    assert review["verdict"]=="INVALIDATED" and review["head_sha"]==result["head"]
    assert not conn.execute("SELECT * FROM reviews WHERE task_id=? AND head_sha=? AND verdict='PASS'",(identifier,result["head"])).fetchone()


def test_failed_refresh_checks_still_preserve_held_exact_candidate(conn,project_root,project,monkeypatch):
    identifier,source,work,old,_=prepare(conn,project_root,project,monkeypatch)
    monkeypatch.setattr(gates,"run_gate",lambda *a,**kw:gates.GateResult("fast",False,skipped_reason="source defect"))
    result=test_refresh.refresh_source(conn,project,identifier,source)
    assert not result["passed"] and db.get_task(conn,identifier)["status"]=="BLOCKED"
    assert git(work,"rev-parse",result["backup"]).stdout.strip()==old
    assert db.latest_checkpoint(conn,identifier,kind="mechanical")["payload"]["head_sha"]==result["head"]


def test_dirty_draft_is_never_rebased(conn,project_root,project,monkeypatch):
    identifier,source,work,old,_=prepare(conn,project_root,project,monkeypatch)
    (work/"services/media.py").write_text("later dirty draft\n")
    with pytest.raises(ValueError,match="clean"):
        test_refresh.refresh_source(conn,project,identifier,source)
    assert repo.head_commit(work)==old


def test_conflicting_accepted_source_retains_original_archive(conn,project_root,project,monkeypatch):
    identifier,source,work,old,accepted=prepare(conn,project_root,project,monkeypatch)
    (accepted/"services/media.py").write_text("VALUE = 'other source'\n")
    git(accepted,"add","services/media.py")
    git(accepted,"commit","-qm","competing source")
    with pytest.raises(ValueError,match="conflicted"):
        test_refresh.refresh_source(conn,project,identifier,source)
    assert repo.head_commit(work)==old and repo.is_clean(work)
    assert git(work,"rev-parse",f"archive/agentkit/source-{identifier}-{old[:12]}").stdout.strip()==old
