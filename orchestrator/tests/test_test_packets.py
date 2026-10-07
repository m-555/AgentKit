"""Real Git handoff checks without launching or contacting an AI provider."""
from dataclasses import replace

import pytest

from agentkit import briefs, db, repo, spec, test_packets
from agentkit.config import ProjectConfig


def config(root, **settings):
    return ProjectConfig(root=root, raw={"workflow": {"mode": "separate-tasks", **settings}},
                         gates={"source": ["echo source"], "tests": ["echo tests"]})


def definitions():
    builder = spec.TaskSpec(spec_id="build", title="Add retry", role="backend-builder",
                            expected_write=["services/retry.py"], gate_level="source",
                            acceptance=["Retry returns a bounded failure on timeout"])
    tester = spec.TaskSpec(spec_id="test", title="Test retry", role="backend-tester",
                           kind="TEST_ONLY", expected_write=["tests/test_retry.py"],
                           expected_read=["services/retry.py"], depends_on=["build"],
                           gate_level="tests")
    return builder, tester


def pair(conn, project_root, *, status="DONE", verdict="PASS", gate=True):
    builder, tester = definitions()
    first = db.create_task(conn, **{
        "title": builder.title, "spec_id": builder.spec_id, "role": builder.role,
        "status": status, "expected_write": builder.expected_write,
        "gate_level": builder.gate_level, "acceptance": builder.acceptance})
    second = db.create_task(conn, title=tester.title, spec_id=tester.spec_id, role=tester.role,
                            status="PLANNED", kind="TEST_ONLY",
                            expected_write=tester.expected_write, expected_read=tester.expected_read,
                            depends_on=tester.depends_on, gate_level="tests")
    head = repo.head_commit(project_root)
    conn.execute("INSERT INTO reviews(task_id,head_sha,verdict,reviewer,evidence,created_at) "
                 "VALUES(?,?,?,?,?,?)", (first, head, verdict, "human", "Inspected commit", db.utcnow()))
    conn.commit()
    if gate:
        db.record_gate(conn, first, "source", head, True, "source check passed")
    return first, second, head


def commit(root, path, content):
    (root / path).write_text(content, encoding="utf-8")
    repo._git(["add", "--", path], root, strict=True)
    repo._git(["commit", "-qm", "Fixture source change"], root, strict=True)


def test_pairing_enforces_same_area_job_and_declared_reads(tmp_path):
    project = config(tmp_path)
    builder, tester = definitions()
    assert test_packets.dependencies(project, tester, [builder, tester]) == [builder]
    with pytest.raises(ValueError, match="source-writing backend-builder"):
        test_packets.dependencies(project, tester, [replace(builder, role="frontend-builder"), tester])
    with pytest.raises(ValueError, match="another job"):
        test_packets.dependencies(project, tester, [replace(builder, job_id="other"), tester])
    with pytest.raises(ValueError, match="read scope"):
        test_packets.dependencies(project, replace(tester, expected_read=[]), [builder, tester])
    with pytest.raises(ValueError, match="missing or ambiguous"):
        test_packets.dependencies(project, tester, [tester])
    assert test_packets.dependencies(ProjectConfig(root=tmp_path), tester, []) == []


def test_tester_ready_and_brief_created_without_planner(conn, project_root):
    first, second, head = pair(conn, project_root)
    project = config(project_root)
    assert second in db.refresh_ready(conn)
    packet = briefs.build(conn, project, second)
    handoff = packet["implementation_handoff"]
    assert handoff[0]["task_id"] == first and handoff[0]["commit"] == head
    assert handoff[0]["source_files"] == ["services/retry.py"]
    text = briefs.render(packet)
    assert "Retry returns a bounded failure on timeout" in text
    assert "Approved commit: " + head in text
    assert "VALUE =" not in text  # Source is available in checkout, not duplicated in prompt.
    test_packets.verify_checkout(conn, project, db.get_task(conn, second), project_root)


@pytest.mark.parametrize("status,verdict,gate", [
    ("REVIEW", "PASS", True), ("DONE", "CHANGES", True), ("DONE", "PASS", False)])
def test_unready_implementation_refuses_tester(conn, project_root, status, verdict, gate):
    _, second, _ = pair(conn, project_root, status=status, verdict=verdict, gate=gate)
    project = config(project_root)
    task = db.get_task(conn, second)
    assert test_packets.build(conn, project, task)[0]["state"] == "waiting"
    with pytest.raises(ValueError, match="awaits merged"):
        test_packets.verify_checkout(conn, project, task, project_root)


def test_changed_approved_source_requires_replan(conn, project_root):
    _, second, _ = pair(conn, project_root)
    commit(project_root, "services/retry.py", "VALUE = 'different'\n")
    with pytest.raises(ValueError, match="changed after approval"):
        test_packets.verify_checkout(conn, config(project_root), db.get_task(conn, second), project_root)


def test_uncommitted_source_changes_refuse_launch(conn, project_root):
    _, second, _ = pair(conn, project_root)
    (project_root / "services/retry.py").write_text("DIRTY = True\n")
    with pytest.raises(ValueError, match="uncommitted changes"):
        test_packets.verify_checkout(conn, config(project_root), db.get_task(conn, second), project_root)


def test_unrelated_integrated_change_does_not_require_replan(conn, project_root):
    _, second, _ = pair(conn, project_root)
    commit(project_root, "services/media.py", "VALUE = 'new media'\n")
    test_packets.verify_checkout(conn, config(project_root), db.get_task(conn, second), project_root)


def test_missing_implementation_ancestry_refuses_launch(conn, project_root, tmp_path):
    _, second, _ = pair(conn, project_root)
    old = tmp_path / "old-checkout"
    repo._git(["worktree", "add", "--detach", str(old), "HEAD~1"], project_root, strict=True)
    with pytest.raises(ValueError, match="does not contain"):
        test_packets.verify_checkout(conn, config(project_root), db.get_task(conn, second), old)


def test_deleted_source_stays_deleted(conn, project_root):
    repo._git(["rm", "--", "services/retry.py"], project_root, strict=True)
    repo._git(["commit", "-qm", "Fixture deletion"], project_root, strict=True)
    _, second, _ = pair(conn, project_root)
    project = config(project_root)
    task = db.get_task(conn, second)
    test_packets.verify_checkout(conn, project, task, project_root)
    (project_root / "services/retry.py").write_text("RECREATED = True\n")
    with pytest.raises(ValueError, match="recreated"):
        test_packets.verify_checkout(conn, project, task, project_root)


def test_packet_cannot_silently_exceed_worker_context(conn, project_root):
    first, second, _ = pair(conn, project_root)
    db.update_task(conn, first, acceptance=["x" * 17000])
    with pytest.raises(ValueError, match="context"):
        briefs.build(conn, config(project_root), second)


def test_bad_tester_definition_leaves_graph_unchanged(project_root):
    from agentkit import planning
    builder, tester = definitions()
    config_path = project_root / ".ai/project.yaml"
    config_path.write_text("workflow: {mode: separate-tasks}\ngates: {source: ['echo source'], tests: ['echo tests']}\n")
    spec.save(project_root, [builder])
    before = (project_root / ".ai/tasks.yaml").read_bytes()
    with pytest.raises(ValueError, match="read scope"):
        planning.put(project_root, replace(tester, expected_read=[]))
    assert (project_root / ".ai/tasks.yaml").read_bytes() == before


def test_source_references_use_normalized_paths(conn, project_root):
    first, second, _ = pair(conn, project_root)
    db.update_task(conn, first, expected_write=["./services/retry.py"])
    db.update_task(conn, second, expected_read=["services/retry.py"])
    project = config(project_root)
    task = db.get_task(conn, second)
    assert test_packets.build(conn, project, task)[0]["source_files"] == ["services/retry.py"]
    test_packets.verify_checkout(conn, project, task, project_root)
