"""The view stays online and can always send.

The user's view went away (nothing restarted it), and the one they started by
hand had no operator controls, so Send failed. `agentkit view` now records that
the view is wanted; after every pass the supervisor restarts it when nothing
answers, and a read-only view of the project is replaced by one that can send.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from agentkit import db, service_launch, view_keeper


def _answering(root, *, operator=True, pid=77):
    return lambda port: {"pid": pid, "root": str(Path(root).resolve()), "operator_controls": operator}


def test_a_wanted_view_that_is_down_is_started_again(project_root):
    view_keeper.save(project_root, {"always_on": True, "port": 8766})
    started = []
    note = view_keeper.heal(project_root, starter=lambda root, port: started.append(port) or 9,
                            prober=lambda port: None)
    assert started == [8766] and "started" in note
    assert view_keeper.state(project_root)["restarted_at"]


def test_a_start_under_way_is_not_repeated_within_a_minute(project_root):
    moment = db.parse_ts(db.utcnow())
    view_keeper.save(project_root, {"always_on": True, "port": 8766, "restarted_at": moment.isoformat()})
    assert view_keeper.heal(project_root, starter=lambda *a: (_ for _ in ()).throw(AssertionError("again")),
                            prober=lambda port: None, now=moment + timedelta(seconds=30)) is None


def test_an_unwanted_or_healthy_view_is_left_alone(project_root):
    never = lambda *a: (_ for _ in ()).throw(AssertionError("started"))  # noqa: E731
    assert view_keeper.heal(project_root, starter=never, prober=lambda port: None) is None  # never asked for
    view_keeper.save(project_root, {"always_on": True, "port": 8766})
    assert view_keeper.heal(project_root, starter=never, prober=_answering(project_root)) is None


def test_a_read_only_view_of_the_project_is_replaced_so_send_works(project_root):
    stopped, started = [], []
    result = view_keeper.ensure(project_root, 8766, starter=lambda root, port: started.append(port) or 5,
                                prober=_answering(project_root, operator=False, pid=2128),
                                stopper=stopped.append, wait=False)
    assert stopped == [2128] and started == [8766] and result == "started 5"


def test_another_projects_view_is_never_touched(project_root, tmp_path):
    other = lambda port: {"pid": 1, "root": str(tmp_path / "other"), "operator_controls": False}  # noqa: E731
    never = lambda *a: (_ for _ in ()).throw(AssertionError("touched"))  # noqa: E731
    assert view_keeper.ensure(project_root, 8766, starter=never, prober=other, stopper=never, wait=False) == "port busy"


def test_the_view_reports_who_serves_the_port(project_root):
    from tests.test_dashboard import request, serving
    with serving(project_root) as server:
        status, _, body = request(server, "/api/view")
    view = json.loads(body)
    assert status == 200 and view["root"] == str(project_root.resolve()) and view["started_at"]
    assert view["operator_controls"] is False and isinstance(view["pid"], int)


def test_a_view_started_without_wmi_opens_no_console_window(project_root, monkeypatch):
    """F34: the fallback launch, used when WMI refuses, hides its console too."""
    monkeypatch.setattr(service_launch, "wmi_create", lambda argv, cwd: None)
    seen = {}
    monkeypatch.setattr(view_keeper.subprocess, "Popen", lambda argv, **k: seen.update(k) or SimpleNamespace(pid=5))
    assert view_keeper.launch(project_root, 8799) == 5
    if os.name == "nt":
        assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert not seen["creationflags"] & subprocess.DETACHED_PROCESS
