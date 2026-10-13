"""The user writes a job in the view, in their own words and with screenshots.

The assigner and the builders open the screenshots with the job_attachment tool;
they are kept with the runtime state, never committed, and only real images are
kept.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit import (
    briefs,
    codex_mcp_policy,
    dashboard,
    db,
    job_input,
    jobs,
    mcp_extra,
    mcp_worker,
)
from tests.test_view_home import call, home_view

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def encoded(data: bytes, url: bool = False) -> dict:
    text = base64.b64encode(data).decode()
    return {"data": ("data:image/png;base64," if url else "") + text}


def test_a_job_keeps_the_words_and_its_screenshots(project_root):
    job = job_input.create(project_root, "Make the export button blue like the screenshot",
                           acceptance=["The button is blue", "  "], images=[encoded(PNG), encoded(JPEG, url=True)])
    assert job["id"] == "make-the-export-button-blue-like"
    assert job["requests"][0] == {**job["requests"][0], "text": "Make the export button blue like the screenshot",
                                  "attachments": ["r1-1.png", "r1-2.jpg"]}
    assert job["acceptance"] == ["The button is blue"]
    folder = job_input.folder(project_root, job["id"])
    assert (folder / "r1-1.png").read_bytes() == PNG and (folder / "r1-2.jpg").read_bytes() == JPEG
    assert folder.is_relative_to(project_root / ".ai" / "runtime")  # never committed


def test_an_update_adds_its_own_screenshots(project_root):
    job = job_input.create(project_root, "Add a dark mode")
    updated = job_input.update(project_root, job["id"], "Use these colors", images=[encoded(PNG)])
    assert updated["revision"] == 2 and updated["requests"][-1]["attachments"] == ["r2-1.png"]
    assert job_input.names(project_root, job["id"]) == ["r2-1.png"]


def test_the_same_words_make_a_new_job_id(project_root):
    first = job_input.create(project_root, "Fix login")
    second = job_input.create(project_root, "Fix login")
    assert (first["id"], second["id"]) == ("fix-login", "fix-login-2")
    assert job_input.create(project_root, "درست کردن ورود")["id"] == "job"


@pytest.mark.parametrize("images, message", [
    ([{"data": base64.b64encode(b"MZ executable").decode()}], "not a PNG"),
    ([{"data": "%%%not-base64"}], "not valid image data"),
    ([{"name": "no data"}], "no image data"),
    ([encoded(PNG)] * 11, "at most 10"),
])
def test_only_real_images_are_kept(project_root, images, message):
    with pytest.raises(ValueError, match=message):
        job_input.create(project_root, "Anything", images=images)
    assert not (project_root / ".ai" / "jobs" / "anything.json").exists()
    assert not job_input.folder(project_root, "anything").exists()


def test_a_job_needs_words(project_root):
    with pytest.raises(ValueError, match="own words"):
        job_input.create(project_root, "   ", images=[encoded(PNG)])


def test_the_tool_opens_a_screenshot_and_nothing_else(project_root, monkeypatch):
    job = job_input.create(project_root, "Match this layout", images=[encoded(PNG)])
    monkeypatch.setenv("AGENTKIT_ROOT", str(project_root))
    image = mcp_extra.job_attachment(job["id"], "r1-1.png")
    assert image.data == PNG
    for name in ("../../tasks.db", "r1-2.png", "r1-1.png/../x.png"):
        with pytest.raises(ValueError, match="no screenshot"):
            mcp_extra.job_attachment(job["id"], name)


def test_builders_and_reviewers_may_open_screenshots():
    assert "job_attachment" in mcp_worker.WORKER_TOOLS
    assert set(codex_mcp_policy.WORKER_TOOLS) == mcp_worker.WORKER_TOOLS
    for role, readonly in (("worker", False), ("reviewer", True), ("coordinator", True)):
        flags = codex_mcp_policy.approval_flags(role, readonly=readonly)
        assert 'mcp_servers.agentkit.tools.job_attachment.approval_mode="approve"' in flags


def test_the_builders_brief_names_the_screenshots(conn, project_root, project):
    job = job_input.create(project_root, "Match this layout", images=[encoded(PNG)])
    task_id = db.create_task(conn, title="Layout", spec_id="layout", job_id=job["id"], status="READY",
                             expected_write=["ui/layout.css"], owned_paths=["ui/layout.css"])
    text = briefs.render(briefs.build(conn, project, task_id), budget=1500)
    assert "## Screenshots from the user" in text and "r1-1.png" in text
    assert f"job_attachment(job_id='{job['id']}'" in text


def test_the_view_saves_a_job_with_screenshots(project_root):
    with home_view(project_root) as server:
        token = call(server, "/api/reviews")[1]["token"]
        post = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        big = encoded(b"\x89PNG\r\n\x1a\n" + b"\x01" * 200_000)  # far over the 16 KB limit of other actions
        status, result = call(server, "/api/job", body={"text": "Rebuild the chart", "acceptance": ["It renders"],
                                                         "images": [big]}, headers=post)
        assert status == 200, result
        assert result["job"] == "rebuild-the-chart" and "1 screenshot" in result["message"]
        assert "Press Start" in result["message"]
        status, result = call(server, "/api/job-update", body={"job_id": "rebuild-the-chart", "text": "Also a legend"},
                              headers=post)
        assert status == 200 and result["job"] == "rebuild-the-chart"
        assert call(server, "/api/job", body={"text": "x"})[0] == 403
        assert call(server, "/api/job", body={"words": "x"}, headers=post)[0] == 409
        data = call(server, "/api/jobs")[1]
    (row,) = data["jobs"]
    assert row["screenshots"] == 1 and row["updates"] == 1
    assert jobs.load(project_root, "rebuild-the-chart")["revision"] == 2


def test_the_form_reads_acceptance_lines():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser-side job form helper")
    source = Path(dashboard.__file__).with_name("dashboard_job_form.js").read_text(encoding="utf-8")
    program = ("const fs = require('fs'); global.window = {};"
               "eval(JSON.parse(fs.readFileSync(0, 'utf8')).source);"
               "console.log(JSON.stringify(window.AgentKitJobForm.lines(' It renders \\r\\n\\n A legend ')));")
    completed = subprocess.run([node, "-e", program], input=json.dumps({"source": source}),
                               text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == ["It renders", "A legend"]


def test_the_assigner_is_told_about_the_screenshots(conn, project_root, monkeypatch):
    from agentkit import processes, supervisor
    from agentkit.config import load_project
    job = job_input.create(project_root, "Match this layout", images=[encoded(PNG)])
    started = []
    monkeypatch.setattr(processes, "start", lambda conn, root, launch, **kwargs: started.append(launch) or 1)
    supervisor._control_launch(conn, load_project(project_root), jobs.load(project_root, job["id"]), "coordinator")
    if not started:
        pytest.skip("no Codex or Claude CLI is installed to build a launch")
    assert "r1-1.png" in started[0].stdin_text and "job_attachment" in started[0].stdin_text
