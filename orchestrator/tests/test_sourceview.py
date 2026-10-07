"""Security and actual MCP stdio registration, using only local source objects."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentkit import db, processes, repo, sourceview
from tests.conftest import commit_all, git


def view(root):
    return sourceview.Scope(root, repo.head_commit(root), "project")


def test_read_does_not_create_an_integration_branch(project_root, conn, project):
    before = git(project_root, "worktree", "list", "--porcelain").stdout
    with pytest.raises(ValueError, match="never create"):
        sourceview.scope(conn, project, where="integration")
    assert not repo.branch_exists(project_root, "integration")
    assert git(project_root, "worktree", "list", "--porcelain").stdout == before
    git(project_root, "branch", "integration")
    result = sourceview.scope(conn, project, where="integration")
    assert result.root == project_root
    assert git(project_root, "worktree", "list", "--porcelain").stdout == before


def test_middle_key_page_and_search_are_redacted(project_root):
    key = "-----BEGIN PRIVATE KEY-----\nSYNTHETICKEYMATERIAL\nSECONDKEYLINE\n-----END PRIVATE KEY-----\n"
    (project_root / "services/retry.py").write_text(key)
    commit_all(project_root, "synthetic embedded key")
    result = sourceview.read(view(project_root), "services/retry.py", start_line=2, max_lines=1)
    assert "SYNTHETIC" not in result["content"] and "redacted" in result["content"]
    assert result["total_lines"] == 4
    assert sourceview.search(view(project_root), "SYNTHETIC")["matches"] == []
    assert "SECONDKEYLINE" not in sourceview.read(view(project_root), "services/retry.py")["content"]


@pytest.mark.parametrize("path", ["../outside", "C:/credentials", "/etc/passwd", ".env", ".ai/runtime/log"])
def test_path_escape_and_secret_paths_are_refused(project_root, path):
    with pytest.raises((ValueError, PermissionError)):
        sourceview.read(view(project_root), path)


def test_only_tracked_regular_scoped_objects_are_served(project_root):
    (project_root / ".env").write_text("TOKEN=synthetic-token-value")
    (project_root / "untracked.txt").write_text("private local notes")
    git(project_root, "update-index", "--add", "--cacheinfo", "120000", repo._git(["hash-object", "-w", "services/media.py"], project_root).strip(), "link")
    git(project_root, "add", ".env")
    git(project_root, "commit", "-qm", "tracked secret and symlink")
    scope = view(project_root)
    paths = [r["path"] for r in sourceview.listing(scope)["files"]]
    assert ".env" not in paths and "link" not in paths and "untracked.txt" not in paths
    scope = sourceview.Scope(project_root, scope.commit, "restricted", allowed=("services/retry.py",))
    assert [r["path"] for r in sourceview.listing(scope)["files"]] == ["services/retry.py"]


def test_review_authority_is_job_task_commit_and_role_scoped(project_root, conn, project, monkeypatch):
    task = db.create_task(conn, title="Review", job_id="job-a", worktree=str(project_root), expected_read=["services/retry.py"])
    sha = repo.head_commit(project_root)
    identifier = conn.execute("INSERT INTO processes(purpose,provider,task_id,job_id,status,expected_head,launch_json,started_at) VALUES('review','codex',?,'job-a','RUNNING',?,'{}',?)", (task, sha, db.utcnow())).lastrowid
    monkeypatch.setenv("AGENTKIT_PROCESS", str(identifier))
    result = sourceview.scope(conn, project)
    assert result.commit == sha and result.allowed == ("services/retry.py",)
    with pytest.raises(PermissionError):
        sourceview.scope(conn, project, where="project")
    with pytest.raises(PermissionError):
        sourceview.scope(conn, project, task_id=task + 1)
    processes.update(conn, identifier, status="FINISHED")
    with pytest.raises(PermissionError):
        sourceview.scope(conn, project)
    monkeypatch.delenv("AGENTKIT_PROCESS")
    monkeypatch.setenv("AGENTKIT_ROLE", "worker")
    with pytest.raises(PermissionError):
        sourceview.scope(conn, project)


def test_actual_module_stdio_lists_and_calls_read_tools(project_root, conn):
    async def exercise():
        server = StdioServerParameters(command=sys.executable, args=["-m", "agentkit.mcp_server"], env={**os.environ, "AGENTKIT_ROOT": str(project_root)})
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name in ("source_read", "source_list", "source_diff", "source_search", "brief", "job_brief"):
                    assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is True
                    assert tools[name].annotations.model_dump(by_alias=True)["destructiveHint"] is False
                assert not (tools["task_define"].annotations and tools["task_define"].annotations.model_dump(by_alias=True)["readOnlyHint"])
                result = await client.call_tool("source_read", {"path": "services/retry.py", "where": "project"})
                assert not result.model_dump(by_alias=True)["isError"]
                assert "VALUE" in json.loads(result.content[0].text)["content"]
    asyncio.run(exercise())


def test_middle_key_diff_and_commit_subject_are_redacted(project_root):
    lines = ["-----BEGIN PRIVATE KEY-----", *["SYNTHETICKEYLINE" + str(i) for i in range(20)], "-----END PRIVATE KEY-----"]
    target = project_root / "services/retry.py"
    target.write_text("\n".join(lines))
    base = commit_all(project_root, "key fixture")
    lines[10] = "NEWSECRETKEYLINE"
    target.write_text("\n".join(lines))
    token = "sk-" + "a" * 25
    commit_all(project_root, "synthetic token " + token)
    result = sourceview.diff(sourceview.Scope(project_root, repo.head_commit(project_root), "task", base))
    encoded = json.dumps(result)
    assert "SYNTHETICKEYLINE" not in encoded and "NEWSECRETKEYLINE" not in encoded and token not in encoded


def test_source_mcp_read_preserves_database_and_git_state(project_root, conn, monkeypatch):
    from agentkit.mcp_extra import source_read
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    before = conn.execute("SELECT total_changes() AS count").fetchone()["count"]
    event_count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    status = git(project_root, "status", "--porcelain").stdout
    source_read("services/retry.py", where="project")
    assert conn.execute("SELECT total_changes() AS count").fetchone()["count"] == before
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == event_count
    assert git(project_root, "status", "--porcelain").stdout == status
