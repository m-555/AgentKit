"""The Jobs tab shows what waits for the user and takes their reply; the rest of the job keeps going."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit import dashboard, dashboard_home, db, problems, project_registry, reviews
from agentkit import statemachine as sm
from tests.test_dashboard import request, serving
from tests.test_infra_stop import DENIED, job_task
from tests.test_view_home import call, home_view


def stopped(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    problems.sync(conn, project)
    return task_id


def test_the_jobs_tab_is_first_and_loads_its_script(project_root):
    with serving(project_root) as server:
        text = request(server, "/")[2].decode("utf-8")
        assert text.index('id="main-tab-jobs"') < text.index('id="main-tab-settings"')
        assert 'id="panel-jobs"' in text and 'id="problem-list"' in text and 'id="job-list"' in text
        assert "/dashboard_jobs.js" in text and request(server, "/dashboard_jobs.js")[0] == 200


def test_the_view_lists_problems_and_jobs(conn, project_root):
    task_id = stopped(conn, project_root)
    with home_view(project_root) as server:
        status, data = call(server, "/api/jobs")
    assert status == 200
    (problem,) = data["problems"]
    assert problem["task_id"] == task_id and problem["options"] == ["retry", "skip", "plan"]
    assert DENIED in problem["detail"] and problem["task_title"] == "Bound retry"
    (job,) = data["jobs"]
    assert job["id"] == "feature" and job["status"] == "ACTIVE" and job["tasks"] == {"BLOCKED": 1}
    assert job["request"] == "Bound the retry delay"


def test_the_repository_list_counts_what_waits(conn, project_root):
    stopped(conn, project_root)
    project_registry.register(project_root)
    row = next(r for r in dashboard_home.repos(project_root)["repos"] if r["selected"])
    assert row["problems"] == 1


def test_a_reply_from_the_view_fixes_the_problem_once(conn, project_root):
    task_id = stopped(conn, project_root)
    problem = problems.listing(conn)[0]["id"]
    with home_view(project_root) as server:
        token = call(server, "/api/reviews")[1]["token"]
        post = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        status, result = call(server, "/api/problem", body={"problem": problem, "action": "retry", "text": ""},
                              headers=post)
        assert status == 200 and "review" in result["message"].lower()
        again = call(server, "/api/problem", body={"problem": problem, "action": "retry", "text": ""}, headers=post)
        assert again[0] == 409 and "not open" in again[1]["message"]
        assert call(server, "/api/problem", body={"problem": problem, "action": "retry"}, headers=post)[0] == 409
        assert call(server, "/api/problem", body={"problem": problem, "action": "retry", "text": ""})[0] == 403
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW


def test_every_reply_has_a_button():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser-side jobs helper")
    source = Path(dashboard.__file__).with_name("dashboard_jobs.js").read_text(encoding="utf-8")
    program = ("const fs = require('fs'); global.window = {};"
               "eval(JSON.parse(fs.readFileSync(0, 'utf8')).source);"
               "console.log(JSON.stringify(Object.keys(window.AgentKitJobs.REPLIES)));")
    completed = subprocess.run([node, "-e", program], input=json.dumps({"source": source}),
                               text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == list(problems.ACTIONS)
