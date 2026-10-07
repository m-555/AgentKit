"""Read-only MCP tools preserve state and never create or migrate its database."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentkit import db, jobs, mcp_extra, mcp_server, mcp_workspaces, repo

READ_CALLS = {
    "workspaces": {},
    "brief": {"task_id": 1}, "task_list": {},
    "lease_check": {"path": "services/retry.py", "task_id": 1},
    "conflict_check": {"paths": ["services/retry.py"]},
    "gate_list": {}, "hotspot_report": {"limit": 2},
    "job_brief": {"job_id": "read-check"}, "task_diff": {"task_id": 1},
    "source_list": {"where": "project"},
    "source_read": {"path": "services/retry.py", "where": "project"},
    "source_search": {"text": "VALUE", "where": "project"},
    "source_diff": {"task_id": 1},
}
DATABASE_CALLS = {name: args for name, args in READ_CALLS.items()
                  if name not in ("gate_list", "hotspot_report")}


@pytest.fixture
def reader_state(project_root, conn, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    for name in ("AGENTKIT_PROCESS", "AGENTKIT_ROLE", "AGENTKIT_TASK"):
        monkeypatch.delenv(name, raising=False)
    jobs.create(project_root, "read-check", "Inspect existing state", "codex")
    head = repo.head_commit(project_root)
    task = db.create_task(conn, title="read-only scope", job_id="read-check",
                          status="RUNNING", branch="main", base_sha=head,
                          worktree=str(project_root), owned_paths=["services/retry.py"],
                          expected_write=["services/retry.py"])
    assert task == 1
    db.try_acquire_leases(conn, task, ["services/retry.py"])
    conn.execute("UPDATE meta SET value='must-not-migrate' WHERE key='schema_version'")
    return project_root, conn


def database_snapshot(root, conn):
    return {
        "bytes": {p.name: p.read_bytes() for p in (root / ".ai").glob("tasks.db*")
                  if not p.name.endswith("-shm")},
        "schema": [tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name")],
        "meta": [tuple(row) for row in conn.execute("SELECT * FROM meta ORDER BY key")],
        "data_version": conn.execute("PRAGMA data_version").fetchone()[0],
    }


@pytest.mark.parametrize("name", READ_CALLS)
def test_annotated_reads_preserve_database_bytes_schema_and_meta(reader_state, name):
    root, conn = reader_state
    before = database_snapshot(root, conn)
    function = getattr(mcp_workspaces if name == "workspaces" else mcp_extra if name.startswith("source_") else mcp_server, name)
    result = function(**READ_CALLS[name])
    assert isinstance(result, str) and result
    assert database_snapshot(root, conn) == before


@pytest.mark.parametrize("name", DATABASE_CALLS)
def test_state_reads_refuse_missing_database_without_creating_it(reader_state, name):
    root, conn = reader_state
    conn.close()
    db.db_path(root).unlink()
    before = {p.relative_to(root): p.read_bytes() for p in (root / ".ai").rglob("*") if p.is_file()}
    function = getattr(mcp_workspaces if name == "workspaces" else mcp_extra if name.startswith("source_") else mcp_server, name)
    with pytest.raises(FileNotFoundError, match="existing AgentKit database"):
        function(**DATABASE_CALLS[name])
    assert {p.relative_to(root): p.read_bytes() for p in (root / ".ai").rglob("*") if p.is_file()} == before


def test_actual_stdio_annotations_and_each_read_preserve_existing_state(reader_state):
    root, conn = reader_state
    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "agentkit.mcp_server"],
                                           env={**os.environ, "AGENTKIT_ROOT": str(root)})
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                annotated = {tool.name for tool in tools if tool.annotations and tool.annotations.model_dump(by_alias=True)["readOnlyHint"]}
                assert annotated == set(READ_CALLS)
                for name, arguments in READ_CALLS.items():
                    before = database_snapshot(root, conn)
                    result = await client.call_tool(name, arguments)
                    assert not result.model_dump(by_alias=True)["isError"], (name, result)
                    assert database_snapshot(root, conn) == before, name
    asyncio.run(exercise())


def test_readonly_connection_reads_current_wal_and_rejects_writes(reader_state):
    root, conn = reader_state
    reader = db.connect_readonly(root)
    try:
        identifier = db.create_task(conn, title="new WAL state", status="READY")
        assert db.get_task(reader, identifier)["title"] == "new WAL state"
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("UPDATE meta SET value='forbidden' WHERE key='schema_version'")
    finally:
        reader.close()
