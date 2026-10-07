"""Lost monitor recovery respects durable exit receipts and terminal races."""
import pytest

from agentkit import db, processes, runner, supervisor


def record(conn, *, code=None, state="RUNNING", ended=None):
    cursor=conn.execute(
        "INSERT INTO processes(purpose,provider,generation,status,pid,child_pid,"
        "launch_json,started_at,exit_code,ended_at,child_launch_state) "
        "VALUES('worker','codex',1,?,111,112,'{}',?,?,?,'EXITED')",
        (state,db.utcnow(),code,ended),
    )
    return processes.get(conn,cursor.lastrowid)


def test_stale_owner_snapshot_cannot_fail_finished_process(conn,project_root,monkeypatch):
    stale=record(conn)
    processes.update(conn,stale["id"],status="FINISHED",exit_code=0,ended_at=db.utcnow())
    monkeypatch.setattr(processes,"owning",lambda _: [stale])
    monkeypatch.setattr(supervisor,"pid_alive",lambda _:False)
    calls=[]
    monkeypatch.setattr(runner,"finish_worker",lambda *args:calls.append(args))
    supervisor.recover_monitors(conn,project_root)
    assert not calls
    assert processes.get(conn,stale["id"])["status"]=="FINISHED"
    assert not processes.get(conn,stale["id"])["error"]


@pytest.mark.parametrize("code,expected",[(0,"FINISHED"),(2,"FAILED"),(None,"FAILED")])
def test_stopped_worker_preserves_exact_exit_receipt(conn,project_root,monkeypatch,code,expected):
    saved=record(conn,code=code)
    monkeypatch.setattr(processes,"owning",lambda _:[saved])
    monkeypatch.setattr(supervisor,"pid_alive",lambda _:False)
    calls=[]
    monkeypatch.setattr(runner,"finish_worker",lambda *args:calls.append(args))
    supervisor.recover_monitors(conn,project_root)
    assert calls[0][3]==(code if code is not None else 1)
    if code==0:
        assert calls[0][4]==""
    else:
        assert calls[0][4]
    result=processes.get(conn,saved["id"])
    assert result["status"]==expected
    if code==0:
        assert not result["error"]
