"""Local HTTP and live fixtures prove the dashboard is a read-only safe view."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import webbrowser
from contextlib import contextmanager
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from agentkit import dashboard, db

STAMP = "2026-10-02T08:00:00+00:00"
SESSION = "0199ed18-12ab-7345-a456-123456789abc"
PRIVATE = "PRIVATE_PROMPT_OUTPUT_AND_REASONING"
SECRET = "sk-ant-" + "a" * 40
COUNTERS = ("input_tokens", "output_tokens", "thinking_tokens",
            "cached_input_tokens", "cache_write_input_tokens")


def process(conn, *, task_id=None, purpose="worker", provider="codex"):
    launch = {"argv": [provider, PRIVATE], "stdin_text": PRIVATE,
              "cwd": "C:/work/session", "env": {"API_TOKEN": SECRET,
              "AGENTKIT_MODEL": "gpt-6.1-sol", "AGENTKIT_MODEL_EFFORT": "xhigh"}}
    identifier = conn.execute(
        "INSERT INTO processes(purpose,task_id,job_id,provider,status,session_token,"
        "launch_json,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (purpose, task_id, "session-job", provider, "RUNNING", SESSION,
         json.dumps(launch), STAMP, STAMP)).lastrowid
    conn.commit()
    return identifier


def stream(root, identifier, events):
    folder = root / ".ai" / "runtime" / f"process-{identifier}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "events.jsonl").write_text("".join(
        json.dumps({"at": STAMP, "channel": "stdout", "text": json.dumps(event)}) + "\n"
        for event in events), encoding="utf-8")


@contextmanager
def serving(root):
    server = dashboard.make_server(root, port=0)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive()


def request(server, path="/", *, method="GET", headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.ids = set()
        self.scripts = []
        self.labels = []
        self.script = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "script":
            self.in_script = True
            if attrs.get("src"):
                self.scripts.append(attrs["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        (self.script if self.in_script else self.labels).append(data)


def test_snapshot_exposes_session_task_activity_and_exact_usage_read_only(project_root, conn):
    task = db.create_task(conn, title="Session overview", status="RUNNING", role="implementer",
                          description="Report current task and verification progress",
                          expected_write=["src/session.py"], expected_read=["docs/contract.md"])
    worker = process(conn, task_id=task)
    coordinator = process(conn, purpose="coordinator", provider="claude-code")
    reviewer = process(conn, purpose="review")
    stream(project_root, worker, [
        {"type": "item.completed", "item": {"type": "reasoning", "text": PRIVATE}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "Read",
         "arguments": {"api_key": SECRET}, "output": PRIVATE}},
        {"type": "turn.completed", "turn_id": "one", "usage": {
         "input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 5}},
    ])
    stream(project_root, coordinator, [{"type": "result", "result": PRIVATE,
           "usage": {"input_tokens": 13, "output_tokens": 4,
                     "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}}])
    before = list(conn.iterdump())
    files = set((project_root / ".ai").rglob("*"))
    view = dashboard.build_snapshot(project_root)
    assert view["status"] == "ok"
    rows = {row["id"]: row for row in view["processes"]}
    assert {rows[p]["role"] for p in (worker, coordinator, reviewer)} == {
        "implementer", "coordinator", "review"}
    assert rows[worker]["session_id"] == SESSION
    assert rows[worker]["model"] == "gpt-6.1-sol" and rows[worker]["effort"] == "xhigh"
    assert rows[worker]["task_id"] == task
    assert any(event.get("name") == "Read" for event in rows[worker]["stream"]["activity"])
    usage = rows[worker]["usage"]
    assert [usage[key] for key in COUNTERS] == [11, 7, None, 5, None]
    assert usage["complete"] and usage["source"]
    assert rows[coordinator]["usage"]["input_tokens"] == 13
    assert all(rows[reviewer]["usage"][key] is None for key in COUNTERS)
    assert not rows[reviewer]["usage"]["complete"]
    overview = next(row for row in view["tasks"] if row["id"] == task)
    assert overview["description"] == "Report current task and verification progress"
    assert overview["expected_write"] == ["src/session.py"]
    assert overview["expected_read"] == ["docs/contract.md"]
    encoded = json.dumps(view)
    assert PRIVATE not in encoded and SECRET not in encoded
    assert "launch_json" not in encoded and "stdin_text" not in encoded
    assert list(conn.iterdump()) == before
    assert set((project_root / ".ai").rglob("*")) == files


def test_task_overview_redacts_credentials(project_root, conn):
    db.create_task(conn, title="Credential redaction", description="API_TOKEN=" + SECRET,
                   expected_write=["src/" + SECRET], expected_read=["docs/" + SECRET])
    view = dashboard.build_snapshot(project_root)
    assert SECRET not in json.dumps(view)
    assert "redacted" in json.dumps(view)


def test_missing_runtime_is_safe_and_never_created(tmp_path):
    root = tmp_path / "missing"
    view = dashboard.build_snapshot(root)
    assert view["status"] == "missing"
    assert view["processes"] == [] and view["tasks"] == []
    with serving(root) as server:
        status, _, body = request(server, "/api/snapshot")
    assert status == 200 and json.loads(body)["status"] == "missing"
    assert not root.exists()


def test_local_server_get_and_head_contract(tmp_path):
    with serving(tmp_path) as server:
        assert server.server_address[0] == "127.0.0.1"
        for path, content_type in (("/", "text/html"), ("/api/snapshot", "application/json")):
            status, headers, body = request(server, path)
            assert status == 200 and content_type in headers["Content-Type"]
            assert body
            status, headers, body = request(server, path, method="HEAD")
            assert status == 200 and content_type in headers["Content-Type"]
            assert body == b""
            assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_mutation_methods_have_no_endpoint(tmp_path, method):
    with serving(tmp_path) as server:
        status, headers, _ = request(server, "/api/snapshot", method=method)
    assert status == 405
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("path", ["/api/cancel", "/api/start", "/tasks", "/.ai/tasks.db"])
def test_only_dashboard_and_snapshot_routes_are_served(tmp_path, path):
    with serving(tmp_path) as server:
        status, _, _ = request(server, path)
    assert status == 404


@pytest.mark.parametrize("headers", [
    {"Host": "remote.example"},
    {"Host": "127.0.0.1.remote.example"},
    {"Origin": "https://remote.example"},
    {"Origin": "null"},
])
def test_cross_origin_or_nonlocal_host_requests_are_refused(tmp_path, headers):
    with serving(tmp_path) as server:
        status, response_headers, _ = request(server, "/api/snapshot", headers=headers)
    assert status in (400, 403)
    assert "Access-Control-Allow-Origin" not in response_headers


def test_runtime_data_is_json_and_rendered_as_text_with_completion_toggle(tmp_path, monkeypatch):
    hostile = '<img src=x onerror="alert(1)"></script><script>HOSTILE_DATA</script>'
    payload = {"status": "ok", "at": STAMP, "message": hostile, "bounded": False,
               "tasks": [{"id": 1, "title": hostile, "status": "DONE"}],
               "processes": [], "external_managers": [], "providers": [], "quota_windows": []}
    monkeypatch.setattr(dashboard, "build_snapshot", lambda root: payload)
    with serving(tmp_path) as server:
        _, headers, body = request(server, "/api/snapshot")
        assert "application/json" in headers["Content-Type"]
        assert json.loads(body)["tasks"][0]["title"] == hostile
        _, _, document = request(server)
    page_text = document.decode("utf-8")
    assert hostile not in page_text and "HOSTILE_DATA" not in page_text
    page = Page()
    page.feed(page_text)
    assert {"tab-working", "tab-waiting", "tab-attention", "tab-history", "history-filter"} <= page.ids
    assert 'role="tablist"' in page_text and 'role="tabpanel"' in page_text
    script = "\n".join(page.script) + Path(dashboard.__file__).with_name("dashboard_view.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "document.write" not in script
    assert "/api/snapshot" in script
    assert {"team-controls", "team-total", "team-current-manager", "team-yaml",
            "team-download"} <= page.ids
    assert "/dashboard_team.js" in page.scripts
    helper = " ".join(page_text.casefold().split())
    assert "claude reported input excludes cache read" in helper and "cache write" in helper
    assert "codex input includes cached" in helper


def test_http_reads_leave_live_database_and_files_unchanged(project_root, conn):
    process(conn)
    before = list(conn.iterdump())
    files = set((project_root / ".ai").rglob("*"))
    with serving(project_root) as server:
        assert request(server, "/")[0] == 200
        status, _, body = request(server, "/api/snapshot")
        assert status == 200 and json.loads(body)["status"] == "ok"
        assert request(server, "/api/snapshot", method="POST")[0] == 405
    assert list(conn.iterdump()) == before
    assert set((project_root / ".ai").rglob("*")) == files


@pytest.mark.parametrize("open_browser", [False, True])
def test_cli_root_port_and_optional_browser_open(tmp_path, monkeypatch, open_browser):
    created, opened, closed = [], [], []

    class FakeServer:
        server_address = ("127.0.0.1", 8123)
        server_port = 8123

        def serve_forever(self, **kwargs):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(True)

        def shutdown(self):
            pass

    def make_server(root, port=8765):
        created.append((root, port))
        return FakeServer()

    monkeypatch.setattr(dashboard, "make_server", make_server)
    monkeypatch.setattr(webbrowser, "open", lambda url, *args, **kwargs: opened.append(url))
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url))
    args = ["--root", str(tmp_path), "--port", "8123"] + (["--open"] if open_browser else [])
    assert dashboard.main(args) == 0
    assert len(created) == 1 and created[0][1] == 8123
    assert str(created[0][0]) == str(tmp_path.resolve())
    assert len(opened) == int(open_browser)
    if open_browser:
        url = urlsplit(opened[0])
        assert (url.scheme, url.hostname, url.port) == ("http", "127.0.0.1", 8123)
    assert closed


def test_cli_cannot_choose_a_nonlocal_bind_address(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid bind argument must never start a server")

    monkeypatch.setattr(dashboard, "make_server", forbidden)
    with pytest.raises(SystemExit) as raised:
        dashboard.main(["--root", str(tmp_path), "--host", "0.0.0.0"])
    assert raised.value.code != 0



def test_team_setup_asset_is_read_only_and_export_stays_client_side(tmp_path):
    with serving(tmp_path) as server:
        status, headers, source = request(server, "/dashboard_team.js")
        assert status == 200 and "javascript" in headers["Content-Type"]
        assert b"AgentKitTeam" in source
        assert request(server, "/dashboard_team.js", method="HEAD")[2] == b""
        assert request(server, "/dashboard_team.js", method="POST")[0] == 405
        assert request(server, "/dashboard_team.js/extra")[0] == 404
        assert request(server, "/api/export")[0] == 404


def test_client_team_choices_counts_and_yaml_export(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute browser-side pure configuration helpers")
    with serving(tmp_path) as server:
        status, _, source = request(server, "/dashboard_team.js")
    assert status == 200
    program = r"""
const fs = require('fs');
global.window = {};
global.document = {getElementById: () => null};
eval(JSON.parse(fs.readFileSync(0, 'utf8')).source);
const team = window.AgentKitTeam;
const pools = ['backend_builders','backend_testers','frontend_builders','frontend_testers'];
const copy = value => JSON.parse(JSON.stringify(value));
const state = {coordinator:{profile:'opus',effort:'xhigh'},reviewer:{profile:'sol',effort:'xhigh'},
 'backend-builder':{profile:'opus',effort:'xhigh'},'backend-tester':{profile:'opus',effort:'xhigh'},
 'frontend-builder':{profile:'sol',effort:'xhigh'},'frontend-tester':{profile:'sol',effort:'xhigh'},
 backend_builders:2,backend_testers:2,frontend_builders:2,frontend_testers:2,
 review_mode:'combined',manager_mode:'supervised',future_project:false,qwen_model:''};
const snapshot = value => ({config:team.configFor(value),total:team.totalSessions(value),
                           yaml:team.toYaml(value)});
const baseline = snapshot(state);
const humanState = copy(state);humanState.workflow_review = "human";delete humanState.reviewer;
const human = snapshot(humanState);
state.review_mode = 'separate';
const separate = snapshot(state);
const independent = pools.map(pool => {const choice = copy(state);choice[pool] = 3;
                                     return snapshot(choice);});
state.coordinator = {profile:'sol',effort:'max'};
state.reviewer = {profile:'opus',effort:'xhigh'};
state.backend_builders = 3;
const selected = snapshot(state);
const efforts = ['medium','high','xhigh','max'].map(effort => {
 const choice = copy(state);choice.coordinator.effort = effort;choice.reviewer.effort = effort;
 return snapshot(choice);
});
for (const pool of pools) state[pool] = 3;
const expanded = snapshot(state);
state.review_mode = 'combined';
const combined = snapshot(state);
const future = copy(state);
future.future_project = true;
future.qwen_model = 'local/qualified-model';
future['backend-builder'] = {profile:'qwen',effort:'max'};
const optedIn = snapshot(future);
const rejected = (value, action=team.configFor) => {
 try {action(value);return false;} catch {return true;}
};
const noOptIn = copy(future);noOptIn.future_project = false;
const denied = [rejected(noOptIn)];
for (const role of ['coordinator','reviewer']) {
 const choice = copy(future);choice[role] = {profile:'qwen',effort:'xhigh'};
 denied.push(rejected(choice));
}
const badCounts = [];
for (const pool of pools) {
 for (const bad of [-1,0,1,4,2.5,true,false,'3','bad',null,undefined]) {
  const choice = copy(state);choice[pool] = bad;
  badCounts.push(rejected(choice) && rejected(choice,team.totalSessions));
 }
}
console.log(JSON.stringify({baseline,human,separate,independent,selected,expanded,combined,
 optedIn,denied,badCounts,efforts,catalog:Object.keys(team.modelCatalog)}));
"""
    completed = subprocess.run([node, "-e", program],
                               input=json.dumps({"source": source.decode("utf-8")}),
                               text=True, capture_output=True, timeout=10, check=True)
    choices = json.loads(completed.stdout)
    assert choices["human"]["total"] == 9
    assert choices["human"]["config"]["workflow"]["review"] == "human"
    assert "reviewer" not in choices["human"]["config"]["model_policy"]["roles"]
    assert yaml.safe_load(choices["human"]["yaml"]) == choices["human"]["config"]
    assert {"opus", "sol"} <= set(choices["catalog"])
    baseline, selected, combined = (choices[key] for key in ("baseline", "selected", "combined"))
    assert baseline["total"] == 9 and baseline["config"]["max_workers"] == 8
    assert choices["separate"]["total"] == 10
    assert all(choice["total"] == 11 and choice["config"]["max_workers"] == 9
               for choice in choices["independent"])
    assert selected["total"] == 11 and selected["config"]["max_workers"] == 9
    assert choices["expanded"]["total"] == 14 and choices["expanded"]["config"]["max_workers"] == 12
    roles = selected["config"]["model_policy"]["roles"]
    assert roles["coordinator"] == {"profile": "sol", "model": "gpt-6.1-sol", "effort": "max"}
    assert roles["reviewer"]["model"] == "claude-opus-5-5"
    combined_roles = combined["config"]["model_policy"]["roles"]
    assert combined["total"] == 13 and combined_roles["reviewer"] == combined_roles["coordinator"]
    worker = choices["optedIn"]["config"]["model_policy"]["assignments"]["backend-builder"]
    assert worker == {"profile": "qwen", "model": "local/qualified-model"}
    assert all(choices["denied"]) and all(choices["badCounts"])
    for effort, choice in zip(("medium", "high", "xhigh", "max"), choices["efforts"], strict=True):
        assert choice["config"]["model_policy"]["roles"]["coordinator"]["effort"] == effort
        assert choice["config"]["model_policy"]["roles"]["reviewer"]["effort"] == effort
        assert yaml.safe_load(choice["yaml"]) == choice["config"]
    for key in ("baseline", "separate", "selected", "expanded", "combined", "optedIn"):
        config = choices[key]["config"]
        assert set(config) == {"max_workers", "workflow", "model_policy"}
        assert config["workflow"]["mode"] == "separate-tasks"
        assert config["workflow"]["review"] == "ai"
        assert sum(config["workflow"]["worker_slots"].values()) == config["max_workers"]
        assert yaml.safe_load(choices[key]["yaml"]) == config
        assert "# Separate-task roles automatically use the matching assignment name" in choices[key]["yaml"]
        assert "# An explicit task model_assignment overrides the role choice." in choices[key]["yaml"]
