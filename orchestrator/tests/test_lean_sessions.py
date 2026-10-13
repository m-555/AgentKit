"""Every session starts lean: no unused catalogs, a short brief, and reads in one turn.

The benchmark's sessions each paid 16-17k tokens before their first action: the
user's plugin and skill catalogs, a sub-agent role block, and a 19k-character
brief, all re-sent on every turn. The worker guard also forced one file read per
shell call, and every extra turn re-sends the whole context.
"""
from __future__ import annotations

import json

import pytest

from agentkit import adapters, briefs, db, mcp_server
from agentkit.adapters.codex import LEAN_CONFIG
from agentkit.codex_hooks import _shell
from agentkit.config import ProjectConfig
from agentkit.leases import Decision


def never(path):
    return Decision(False, "read-only", "test")


@pytest.mark.parametrize("role", ["backend-builder", "reviewer", "coordinator"])
def test_codex_sessions_carry_no_catalog_agentkit_never_uses(tmp_path, monkeypatch, role):
    adapter = adapters.get("codex")
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"id": 1}, tmp_path, role, ProjectConfig(root=tmp_path), prompt="Task")
    pairs = {launch.argv[i + 1] for i, value in enumerate(launch.argv[:-1]) if value == "-c"}
    assert set(LEAN_CONFIG) <= pairs
    assert {"features.multi_agent=false", "skills.include_instructions=false", "features.plugins=false",
            "features.apps=false"} <= set(LEAN_CONFIG)


@pytest.mark.parametrize("role", ["backend-builder", "reviewer"])
def test_claude_sessions_skip_skill_listing_and_keep_a_shared_prompt_prefix(tmp_path, monkeypatch, role):
    adapter = adapters.get("claude-code")
    monkeypatch.setattr(adapter, "detect", lambda: None)
    launch = adapter.build_launch({"id": 1}, tmp_path, role, ProjectConfig(root=tmp_path), prompt="Task")
    assert "--disable-slash-commands" in launch.argv
    assert "--exclude-dynamic-system-prompt-sections" in launch.argv


@pytest.mark.parametrize("command", [
    "Get-Content -LiteralPath a.py; Get-Content -LiteralPath b.py",
    "cat a.py; cat b.py; wc -l c.py",
    "Get-Content -LiteralPath 'name;with;semicolons.txt'",
    "rg --files; Get-ChildItem -LiteralPath docs",
])
def test_several_reads_may_share_one_command(command):
    verdict = _shell(command, never, [])
    assert verdict.allowed and not verdict.writes


@pytest.mark.parametrize("command", [
    "Get-Content -LiteralPath a.py; Set-Content -LiteralPath a.py -Value x",
    "cat a.py; rm a.py",
    "cat a.py; git status",
    "cat a.py > b.py; cat c.py",
    "Get-Content -LiteralPath a.py | Set-Content b.py",
    "Get-Content -LiteralPath $env:SECRET; cat a.py",
    "cat a.py;; cat b.py",
    "cat a.py;",
    "rg --pre evil x; cat a.py",
])
def test_a_batch_with_anything_but_reads_is_still_refused(command):
    assert not _shell(command, never, []).allowed


def task_with_context(conn, project_root, *, goal="Bound the retry delay to the configured limit."):
    others = []
    for index in range(40):
        others.append(db.create_task(conn, title=f"Parallel work {index} on a long descriptive title",
                                     status="RUNNING", owned_paths=[f"services/other_{index}.py"]))
        db.try_acquire_leases(conn, others[-1], [f"services/other_{index}.py"], mode="exclusive-write")
    return db.create_task(conn, title="Bound retry", description=goal, status="RUNNING", generation=1,
                          owned_paths=["services/retry.py"], expected_write=["services/retry.py"],
                          acceptance=["Delay never exceeds the limit", "Existing callers keep working"])


def test_the_brief_is_short_by_default_and_complete_on_request(conn, project_root, monkeypatch):
    task_id = task_with_context(conn, project_root)
    for name in ("AGENTKIT_TASK", "AGENTKIT_PROCESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    full = mcp_server.brief(task_id, full=True)
    short = mcp_server.brief(task_id)
    assert len(full) > briefs.BUDGET >= len(short)
    for essential in ("Bound the retry delay", "Delay never exceeds the limit", "services/retry.py",
                      "Gate before you stop"):
        assert essential in short
    assert "Held by other agents" in full and "Held by other agents" not in short
    assert "full=true" in short


def test_essentials_are_never_cut_to_meet_the_budget(conn, project_root, monkeypatch):
    goal = "Keep every requirement. " * 400
    task_id = task_with_context(conn, project_root, goal=goal)
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    monkeypatch.delenv("AGENTKIT_TASK", raising=False)
    short = mcp_server.brief(task_id)
    assert goal.strip() in short and "Delay never exceeds the limit" in short


def test_render_without_a_budget_is_unchanged(conn, project_root):
    from agentkit.config import load_project
    task_id = task_with_context(conn, project_root)
    data = briefs.build(conn, load_project(project_root), task_id)
    assert briefs.render(data) == briefs.render(data, budget=None)
    assert "Omitted" not in briefs.render(data)


def test_source_read_returns_several_files_in_one_call(project_root, conn, monkeypatch):
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    for name in ("AGENTKIT_TASK", "AGENTKIT_PROCESS", "AGENTKIT_ROLE"):
        monkeypatch.delenv(name, raising=False)
    from agentkit.mcp_extra import source_read
    result = json.loads(source_read(paths=["services/retry.py", "services/media.py"], where="project"))
    assert [item["path"] for item in result] == ["services/retry.py", "services/media.py"]
    assert "retry" in json.dumps(result[0]) and "media" in json.dumps(result[1])
    with pytest.raises(ValueError):
        source_read(paths=[f"f{i}.py" for i in range(20)], where="project")


def test_worker_prompt_asks_for_reads_in_one_turn_and_no_tool_discovery():
    from agentkit.scheduler import WORKER_PROMPT
    assert "in one turn" in WORKER_PROMPT
    assert "Get-Content -LiteralPath a; Get-Content -LiteralPath b" in WORKER_PROMPT
    assert "tools.mcp__agentkit__brief" in WORKER_PROMPT and "ALL_TOOLS" in WORKER_PROMPT
