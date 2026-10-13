"""One view for every AgentKit repository: its list, its settings and its Start button.

No model calls: the service, the router and the browser are replaced or driven
locally; settings are read from and written to real project files.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

import pytest
import yaml

from agentkit import (
    dashboard,
    dashboard_home,
    db,
    jobs,
    manager,
    manager_state,
    project_registry,
    providers,
    repo_controls,
    repo_settings,
    view_keeper,
)

POLICY = {
    "workflow": {"mode": "separate-tasks", "review": "ai",
                 "worker_slots": {"backend-builder": 3, "backend-tester": 3}},
    "max_workers": 4,
    "model_policy": {
        "roles": {"coordinator": {"profile": "opus", "model": "claude-opus-5-5", "effort": "high"},
                  "reviewer": {"profile": "opus", "model": "claude-opus-5-5", "effort": "high"}},
        "assignments": {
            "backend-builder": {"profile": "opus", "model": "claude-opus-5-5", "effort": "high"},
            "qwen-builder": {"profile": "qwen", "model": "local/qwen3.8-27b-q8-tuber", "roles": ["backend-builder"]},
        },
    },
}


def agentkit_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".ai").mkdir()
    (path / ".ai" / "project.yaml").write_text("name: x\n", encoding="utf-8")
    return path


@pytest.fixture
def configured(project_root):
    raw = yaml.safe_load((project_root / ".ai" / "project.yaml").read_text(encoding="utf-8"))
    raw.update(json.loads(json.dumps(POLICY)))
    (project_root / ".ai" / "project.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return project_root


def saved(root: Path) -> dict:
    return yaml.safe_load((root / ".ai" / "project.yaml").read_text(encoding="utf-8"))


def test_registry_lists_known_repositories_and_their_agentkit_siblings(tmp_path):
    first = agentkit_repo(tmp_path / "projects" / "first")
    sibling = agentkit_repo(tmp_path / "projects" / "sibling")
    (tmp_path / "projects" / "plain" / ".git").mkdir(parents=True)
    worktree = tmp_path / "projects" / "wt-first-task"
    (worktree / ".ai").mkdir(parents=True)
    (worktree / ".ai" / "project.yaml").write_text("name: x\n", encoding="utf-8")
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert project_registry.projects() == []
    project_registry.register(tmp_path / "projects" / "plain")  # no AgentKit: never listed
    project_registry.register(first)
    rows = project_registry.projects()
    assert [row["name"] for row in rows] == ["first", "sibling"]
    assert rows[0]["last_used_at"] and rows[1]["last_used_at"] is None
    assert project_registry.resolve(rows[1]["id"]) == sibling.resolve()
    assert project_registry.resolve("../../etc") is None
    project_registry.register(sibling)
    assert project_registry.projects()[0]["name"] == "sibling"
    assert (project_registry.home() / "projects.json").is_file()


def test_the_cookie_selects_only_a_listed_repository(tmp_path):
    default = agentkit_repo(tmp_path / "a")
    other = agentkit_repo(tmp_path / "b")
    project_registry.register(default)
    chosen = project_registry.ident(other)
    assert dashboard_home.selected(default, f"agentkit_repo={chosen}", home=True) == other.resolve()
    assert dashboard_home.selected(default, f"agentkit_repo={chosen}", home=False) == default
    for header in ("agentkit_repo=C:/Windows", "agentkit_repo=unknown12345", "garbage;;=", None):
        assert dashboard_home.selected(default, header, home=True) == default


def test_settings_show_every_manager_choice(configured):
    data = repo_settings.read(configured)
    assert data["manager"] == {"profile": "opus", "model": "claude-opus-5-5", "effort": "high"}
    assert data["assignments"]["qwen-builder"]["roles"] == ["backend-builder"]
    assert data["assignments"]["qwen-builder"]["effort"] is None
    assert data["worker_slots"] == {"backend-builder": 3, "backend-tester": 3} and data["max_workers"] == 4
    assert data["choices"]["control"] == ["sol", "opus"]
    assert "astra" not in json.dumps(data["choices"])
    assert [row["provider"] for row in data["providers"]] == ["codex", "claude-code", "local-opencode"]
    assert data["service"]["state"] == "stopped" and data["blocked"] is None
    assert set(data["profiles"]) == {"sol", "opus", "sonnet", "qwen"}


def test_a_stopped_repository_saves_only_the_changed_settings(configured):
    result = repo_settings.apply(configured, {
        "manager": {"profile": "sol", "effort": "xhigh"},
        "assignments": {"backend-builder": {"profile": "sol", "effort": "high"}, "qwen-builder": {"profile": "qwen"}},
        "worker_slots": {"backend-builder": 2}, "max_workers": 6, "visible_windows": True,
        "max_turns": 24, "profiles": {"sonnet": False}, "review": "human"})
    assert result["repinned"] == []
    raw = saved(configured)
    assert raw["model_policy"]["roles"]["coordinator"] == {"profile": "sol", "model": "gpt-6.1-sol", "effort": "xhigh"}
    assert raw["model_policy"]["roles"]["reviewer"]["profile"] == "opus"
    assert raw["model_policy"]["assignments"]["backend-builder"] == {"profile": "sol", "model": "gpt-6.1-sol", "effort": "high"}
    assert raw["model_policy"]["assignments"]["qwen-builder"] == POLICY["model_policy"]["assignments"]["qwen-builder"]
    assert raw["workflow"]["worker_slots"] == {"backend-builder": 2, "backend-tester": 3}
    assert raw["workflow"]["review"] == "human" and raw["workflow"]["mode"] == "separate-tasks"
    assert raw["max_workers"] == 6 and raw["visible_windows"] is True
    assert raw["execution_limits"]["max_turns"] == 24
    assert raw["model_policy"]["profiles"]["sonnet"]["enabled"] is False


@pytest.mark.parametrize("changes", [
    {"manager": {"profile": "astra", "effort": "high"}},
    {"manager": {"profile": "qwen"}},
    {"reviewer": {"profile": "sol", "effort": "minimal"}},
    {"assignments": {"made-up": {"profile": "sol", "effort": "high"}}},
    {"assignments": {"backend-builder": {"profile": "astra", "effort": "high"}}},
    {"worker_slots": {"backend-builder": 9}},
    {"max_workers": -1},
    {"max_turns": 0},
    {"profiles": {"opus": False}},  # the manager and builders use Opus
    {"execution_paused": False},
    {},
])
def test_invalid_settings_change_nothing(configured, changes):
    before = (configured / ".ai" / "project.yaml").read_bytes()
    with pytest.raises(ValueError):
        repo_settings.apply(configured, changes)
    assert (configured / ".ai" / "project.yaml").read_bytes() == before


def test_settings_wait_until_the_repository_is_stopped(configured, monkeypatch):
    from agentkit import service_control
    before = (configured / ".ai" / "project.yaml").read_bytes()
    running = {**service_control.status(configured), "guardian_alive": True, "state": "running"}
    monkeypatch.setattr(service_control, "status", lambda root: running)
    assert repo_settings.read(configured)["blocked"].startswith("Stop this repository")
    with pytest.raises(PermissionError, match="Stop this repository"):
        repo_settings.apply(configured, {"max_workers": 2})
    assert (configured / ".ai" / "project.yaml").read_bytes() == before


def test_a_worker_or_coordinator_cannot_change_settings_or_start(configured, monkeypatch):
    monkeypatch.setenv("AGENTKIT_TASK", "7")
    with pytest.raises(PermissionError):
        repo_settings.apply(configured, {"max_workers": 2})
    with pytest.raises(PermissionError):
        repo_controls.start(configured)


@pytest.fixture
def pinned_job(configured, conn):
    jobs.create(configured, "work", "Keep building", "claude-code")
    from agentkit import models
    jobs.pin_coordinator(configured, "work", models.profile(None, "opus", control=True))
    conn.execute("UPDATE jobs SET status='ACTIVE' WHERE id='work'")
    return "work"


def test_a_new_manager_re_pins_unfinished_jobs(configured, conn, pinned_job):
    before = manager_state.state(conn, pinned_job)["epoch"]
    result = repo_settings.apply(configured, {"manager": {"profile": "sol", "effort": "high"}})
    assert result["repinned"] == [pinned_job]
    pin = jobs.load(configured, pinned_job)["coordinator_model"]
    assert (pin["provider"], pin["model"], pin["effort"]) == ("codex", "gpt-6.1-sol", "high")
    assert manager_state.state(conn, pinned_job)["epoch"] == before + 1
    assert "AgentKit view" in jobs.load(configured, pinned_job)["decisions"][-1]["text"]
    again = repo_settings.apply(configured, {"manager": {"profile": "sol", "effort": "high"}})
    assert again["repinned"] == []


def test_a_chat_manager_with_a_fresh_lease_keeps_the_job(configured, conn, pinned_job):
    conn.execute("INSERT INTO manager_leases(job_id,holder,token_hash,provider,model,effort,session_ref,pid,ttl_seconds,"
                 "acquired_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (pinned_job, "chat", "0" * 64, "claude-code", "claude-opus-5-5", "high", "s", os.getpid(), 3600,
                  db.utcnow(), db.utcnow()))
    before = (configured / ".ai" / "project.yaml").read_bytes()
    with pytest.raises(PermissionError, match="fresh lease"):
        repo_settings.apply(configured, {"manager": {"profile": "sol", "effort": "high"}})
    assert jobs.load(configured, pinned_job)["coordinator_model"]["provider"] == "claude-code"
    assert (configured / ".ai" / "project.yaml").read_bytes() == before
    shown = repo_settings.read(configured)["managers"][0]
    assert shown["lease"]["holder"] == "chat" and shown["lease"]["fresh"]
    assert manager.lease(conn, pinned_job)["holder"] == "chat"


def test_start_lifts_the_pause_and_starts_the_service(configured, monkeypatch):
    from agentkit import service
    raw = saved(configured)
    raw["execution_paused"] = True
    (configured / ".ai" / "project.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    started = []
    monkeypatch.setattr(service, "start", lambda root, max_workers=None: started.append(Path(root)) or "Supervisor started.")
    result = repo_controls.start(configured)
    assert started == [configured] and result["message"] == "Supervisor started."
    assert saved(configured)["execution_paused"] is False
    assert project_registry.projects()[0]["root"] == str(configured.resolve())


def test_stop_ends_the_service(configured, monkeypatch):
    from agentkit import service_control
    monkeypatch.setattr(service_control, "stop", lambda root: {"stopped": [11, 12], "note": ""})
    assert repo_controls.stop(configured)["message"].startswith("Stopped.")


def test_provider_hold_and_clear(configured, conn):
    with pytest.raises(ValueError):
        repo_controls.provider(configured, "codex", "hold", "  ")
    with pytest.raises(ValueError):
        repo_controls.provider(configured, "somebody", "hold", "why")
    assert repo_controls.provider(configured, "codex", "hold", "Not now")["status"] == "HELD"
    assert providers.get_state(conn, "codex").status == "HELD"
    assert repo_controls.provider(configured, "codex", "clear")["status"] == "AVAILABLE"


def test_local_thinking_switch_uses_the_router(monkeypatch):
    calls = []
    state = {"thinkingOverride": None}

    def router(path, body=None, timeout=5):
        calls.append((path, body))
        if body is not None:
            state["thinkingOverride"] = None if body["enabled"] == "default" else body["enabled"]
        return dict(state)
    monkeypatch.setattr(repo_controls, "_call", router)
    assert repo_controls.thinking() == {"available": True, "mode": "default"}
    assert repo_controls.set_thinking("on")["mode"] == "on" and ("/thinking", {"enabled": True}) in calls
    assert repo_controls.set_thinking("off")["mode"] == "off"
    with pytest.raises(ValueError):
        repo_controls.set_thinking("maybe")

    def down(path, body=None, timeout=5):
        raise OSError("connection refused")
    monkeypatch.setattr(repo_controls, "_call", down)
    assert repo_controls.thinking()["available"] is False


@contextmanager
def home_view(root):
    server = dashboard.make_server(root, port=0, operator_controls=True, home=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def call(server, path, *, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request("POST" if payload is not None else "GET", path, body=payload, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


def test_one_view_serves_every_repository(configured, tmp_path, monkeypatch):
    other = agentkit_repo(configured.parent / "other")
    project_registry.register(configured)
    monkeypatch.setattr(repo_controls, "_call", lambda path, body=None, timeout=5: {"thinkingOverride": True})
    cookie = {"Cookie": "agentkit_repo=" + project_registry.ident(other)}
    with home_view(configured) as server:
        status, view = call(server, "/api/view")
        assert status == 200 and view["home"] is True and str(other.resolve()) in view["roots"]
        status, listed = call(server, "/api/repos")
        assert [row["name"] for row in listed["repos"] if row["selected"]] == [configured.name]
        status, listed = call(server, "/api/repos", headers=cookie)
        assert [row["name"] for row in listed["repos"] if row["selected"]] == ["other"]
        status, data = call(server, "/api/settings")
        assert status == 200 and data["name"] == configured.name and data["local_thinking"]["mode"] == "on"
        token = call(server, "/api/reviews")[1]["token"]
        post = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        status, result = call(server, "/api/settings", body={"settings": {"max_workers": 5}}, headers=post)
        assert status == 200 and result["saved"] == ["max_workers"] and saved(configured)["max_workers"] == 5
        status, result = call(server, "/api/settings", body={"settings": {"max_workers": -5}}, headers=post)
        assert status == 409 and saved(configured)["max_workers"] == 5
        assert call(server, "/api/repo-start", body={}, headers={"Content-Type": "application/json"})[0] == 403
        status, result = call(server, "/api/provider", body={"provider": "codex", "action": "hold", "reason": "pause"},
                              headers=post)
        assert status == 200 and result["status"] == "HELD"


def test_without_operator_controls_the_view_changes_nothing(configured):
    with dashboard_server(configured) as server:
        status, _ = call(server, "/api/settings", body={"settings": {"max_workers": 5}},
                         headers={"Content-Type": "application/json"})
    assert status == 405 and saved(configured)["max_workers"] == 4


@contextmanager
def dashboard_server(root):
    server = dashboard.make_server(root, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_a_home_view_counts_as_running_for_any_repository(tmp_path):
    root = agentkit_repo(tmp_path / "a")
    stopped, started = [], []
    home = {"pid": 5, "root": "elsewhere", "operator_controls": True, "home": True, "roots": []}
    assert view_keeper.ensure(root, 8766, prober=lambda port: home, starter=lambda *a: started.append(a),
                              stopper=stopped.append, wait=False) == "running"
    single = {"pid": 6, "root": str(root.resolve()), "operator_controls": True}
    result = view_keeper.ensure(root, 8766, prober=lambda port: single, starter=lambda *a: started.append(a) or 7,
                                stopper=stopped.append, wait=False)
    assert result == "started 7" and stopped == [6]
    other = {"pid": 8, "root": "C:/another", "operator_controls": True}
    assert view_keeper.ensure(root, 8766, prober=lambda port: other, starter=lambda *a: 9,
                              stopper=stopped.append, wait=False) == "port busy"


def test_client_saves_only_what_changed():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser-side settings helper")
    source = (Path(dashboard.__file__).with_name("dashboard_settings.js")).read_text(encoding="utf-8")
    program = r"""
const fs = require('fs');
global.window = {};
eval(JSON.parse(fs.readFileSync(0, 'utf8')).source);
const {changes} = window.AgentKitSettings;
const saved = {manager:{profile:'opus',model:'claude-opus-5-5',effort:'high'},
  reviewer:{profile:'opus',model:'claude-opus-5-5',effort:'high'}, review:'ai',
  assignments:{'backend-builder':{profile:'opus',effort:'high',roles:[]},'qwen-builder':{profile:'qwen',effort:null}},
  worker_slots:{'backend-builder':3}, max_workers:4, visible_windows:true, max_turns:32,
  profiles:{sol:true,opus:true,sonnet:true,qwen:true}};
const form = {manager:{profile:'opus',effort:'high'}, reviewer:{profile:'opus',effort:'high'}, review:'ai',
  assignments:{'backend-builder':{profile:'opus',effort:'high'},'qwen-builder':{profile:'qwen'}},
  worker_slots:{'backend-builder':3}, max_workers:4, visible_windows:true, max_turns:32,
  profiles:{sol:true,opus:true,sonnet:true,qwen:true}};
const untouched = changes(saved, form);
const edited = JSON.parse(JSON.stringify(form));
edited.manager = {profile:'sol', effort:'xhigh'};
edited.assignments['qwen-builder'] = {profile:'opus', effort:'high'};
edited.worker_slots['backend-tester'] = 2;
edited.profiles.sonnet = false;
edited.max_turns = 20;
console.log(JSON.stringify({untouched, edited: changes(saved, edited)}));
"""
    completed = subprocess.run([node, "-e", program], input=json.dumps({"source": source}),
                               text=True, capture_output=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result["untouched"] == {}
    assert result["edited"] == {"manager": {"profile": "sol", "effort": "xhigh"},
                                "assignments": {"qwen-builder": {"profile": "opus", "effort": "high"}},
                                "worker_slots": {"backend-tester": 2}, "profiles": {"sonnet": False},
                                "max_turns": 20}
