"""Reviewers must know declared contracts without opening out-of-scope config."""
from agentkit import briefs, db
from agentkit.config import ProjectConfig


def packet(conn, project_root, contracts):
    project = ProjectConfig(project_root, name="independent-project", contracts=contracts)
    task = db.create_task(conn, title="Source", role="backend-builder", status="READY")
    return briefs.build(conn, project, task)


def test_brief_exposes_declared_contract_paths(conn, project_root):
    brief = packet(conn, project_root, ["contracts/api.json"])
    assert brief["project"]["contracts"] == ["contracts/api.json"]
    assert "contracts/api.json" in briefs.render(brief)


def test_empty_contract_list_is_explicit_not_unknown(conn, project_root):
    brief = packet(conn, project_root, [])
    assert brief["project"]["contracts"] == []
    assert "none declared" in briefs.render(brief)
