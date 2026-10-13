"""The view is tabs at the top, a monitor-style manager screen, and an Agents tab.

The user had to scroll a long page. Each part is now one tab; the manager's
conversation and every agent's words show on monitor screens; only the manager
takes input.
"""
from __future__ import annotations

import json
import os
import re

from agentkit import db
from tests.test_dashboard import request, serving

TABS = ("jobs", "settings", "manager", "agents", "sessions", "usage", "reviews", "setup")


def test_every_part_of_the_view_is_one_tab_at_the_top(project_root):
    with serving(project_root) as server:
        status, _, page = request(server, "/")
        assert status == 200
        text = page.decode("utf-8")
        for name in TABS:
            assert f'id="main-tab-{name}"' in text and f'id="panel-{name}"' in text
        assert text.index('class="main-tabs"') < text.index('id="panel-manager"')
        for asset in ("/dashboard_tabs.js", "/dashboard_agents.js", "/dashboard_chat.js", "/dashboard_settings.js"):
            assert asset in text and request(server, asset)[0] == 200
    # Existing parts keep their ids, now inside panels.
    for kept in ("tab-working", "history-filter", "team-controls", "workspace-list", "recovery", "allowance",
                 "review-jobs", "project-usage", "live-screens", "chat-messages", "chat-text", "chat-job",
                 "settings-form", "repo-select", "repo-start", "repo-stop"):
        assert f'id="{kept}"' in text


def test_the_manager_screen_is_a_monitor_with_a_prompt(project_root):
    with serving(project_root) as server:
        text = request(server, "/")[2].decode("utf-8")
    manager = text[text.index('id="panel-manager"'):text.index('id="panel-agents"')]
    assert 'class="monitor-screen" id="chat-messages"' in manager and 'id="chat-text"' in manager
    agents = text[text.index('id="panel-agents"'):text.index('id="panel-usage"')]
    assert 'id="agent-screen"' in agents and "<textarea" not in agents  # agents are read-only


def test_the_agents_list_puts_working_agents_first(project_root, conn):
    started = db.utcnow()
    task = db.create_task(conn, title="Build the selection view", role="backend-builder", status="RUNNING")
    for purpose, status, task_id in (("coordinator", "FINISHED", None), ("worker", "RUNNING", task),
                                     ("worker", "FINISHED", None)):
        conn.execute("INSERT INTO processes(purpose,provider,task_id,job_id,status,pid,launch_json,started_at) "
                     "VALUES(?,?,?,?,?,?,'{}',?)", (purpose, "claude-code", task_id, "job", status, os.getpid(), started))
    with serving(project_root) as server:
        status, _, body = request(server, "/api/agents")
    agents = json.loads(body)["agents"]
    assert status == 200 and agents[0]["running"] and agents[0]["speaker"] == "Builder"
    assert agents[0]["title"] == "Build the selection view"
    assert any(agent["speaker"] == "Manager" for agent in agents)


def test_view_scripts_render_text_never_html():
    from pathlib import Path

    from agentkit import dashboard
    folder = Path(dashboard.__file__).parent
    for name in ("dashboard_tabs.js", "dashboard_agents.js", "dashboard_chat.js", "dashboard_settings.js"):
        script = (folder / name).read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|insertAdjacentHTML|document\.write", script), name
