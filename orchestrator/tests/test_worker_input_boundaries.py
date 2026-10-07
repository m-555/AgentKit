"""New outputs are not required inputs; revised READY dependencies remain enforced."""
from agentkit import briefs, db, scheduler


def test_new_deliverable_is_not_a_missing_required_input(conn, project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    identifier = db.create_task(conn, title="new route", role="backend-builder",
        expected_read=["AGENTS.md"], expected_write=["routes/new_route.py"],
        owned_paths=["routes/new_route.py"])
    brief = briefs.build(conn, project, identifier)
    assert brief["expected_read"] == ["AGENTS.md"]
    assert brief["expected_write"] == ["routes/new_route.py"]
    assert "routes/new_route.py" in brief["readable_paths"]
    rendered = briefs.render(brief)
    assert "Deliverables (create missing output files)" in rendered
    inputs = rendered.split("## Required inputs", 1)[1].split("##", 1)[0]
    assert "routes/new_route.py" not in inputs


def test_ready_task_cannot_ignore_new_unfinished_dependency(conn):
    source = db.create_task(conn, spec_id="source", title="source", status="PLANNED")
    target = db.create_task(conn, title="client", status="READY", depends_on=["source"])
    assert target not in {task["id"] for task in scheduler.eligible_tasks(conn)}
    conn.execute("UPDATE tasks SET status='DONE' WHERE id=?", (source,))
    assert target in {task["id"] for task in scheduler.eligible_tasks(conn)}
