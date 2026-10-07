"""Host-side scope enforcement avoids interpreting virtual readonly masks as edits."""

from agentkit import db, hooks_cli, monitor_audit


def enable(root):
    path = root / ".ai/project.yaml"
    path.write_text(path.read_text() + "\nworkflow: {mode: separate-tasks}\n")


def test_host_audit_blocks_real_outside_changes_and_preserves_them(project_root, conn):
    enable(project_root)
    task = db.create_task(conn, title="Bounded source", owned_paths=["services/retry.py", ".ai/project.yaml"],
                          status="RUNNING", generation=1)
    process = {"purpose": "worker", "task_id": task, "generation": 1}
    (project_root / "services/retry.py").write_text("value = 2\n")
    assert monitor_audit.check(conn, project_root, process, project_root) == ""
    (project_root / "services/media.py").write_text("preserved = True\n")
    reason = monitor_audit.check(conn, project_root, process, project_root)
    assert "host lease audit" in reason and "services/media.py" in reason
    assert db.get_task(conn, task)["status"] == "BLOCKED"
    assert (project_root / "services/media.py").read_text() == "preserved = True\n"
    count = conn.execute("SELECT count(*) FROM violations").fetchone()[0]
    assert monitor_audit.check(conn, project_root, process, project_root) == ""
    assert conn.execute("SELECT count(*) FROM violations").fetchone()[0] == count


def test_stale_generation_cannot_block_current_owner(project_root, conn):
    enable(project_root)
    task = db.create_task(conn, title="Current", owned_paths=["services/retry.py", ".ai/project.yaml"],
                          status="RUNNING", generation=2)
    (project_root / "services/media.py").write_text("foreign = True\n")
    process = {"purpose": "worker", "task_id": task, "generation": 1}
    assert monitor_audit.check(conn, project_root, process, project_root) == ""
    assert db.get_task(conn, task)["status"] == "RUNNING"


def test_masked_worker_hook_defers_only_when_real_host_audit_is_configured(project_root, conn, monkeypatch):
    enable(project_root)
    task = db.create_task(conn, title="Bounded", owned_paths=["services/retry.py", ".ai/project.yaml"], status="RUNNING")
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("AGENTKIT_TASK", str(task))
    monkeypatch.setenv("AGENTKIT_AUDIT_OWNER", "monitor")
    monkeypatch.setattr(db, "connect", lambda *a: (_ for _ in ()).throw(AssertionError("worker-side audit")))
    assert hooks_cli.handle_post_tool_use({"cwd": str(project_root)}) == 0
