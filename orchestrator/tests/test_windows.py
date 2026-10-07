"""Viewers follow liveness, not recorded completion, and never own workers."""
from __future__ import annotations

import ctypes
import io
import os
from contextlib import contextmanager
from types import SimpleNamespace

from agentkit import db, windows


def view(*rows, status="ok"):
    return {"status": status, "processes": list(rows), "tasks": [], "external_managers": []}


def row(identifier, *, status="RUNNING", monitor=True, child=False):
    return {"id": identifier, "status": status, "monitor_alive": monitor,
            "child_alive": child, "task_id": None, "role": "worker",
            "model": "gpt-6.1-sol", "effort": "xhigh"}


def test_stopped_assignment_closes_despite_other_worker(monkeypatch):
    snapshots = iter([view(row(1), row(2)), view(row(1, monitor=False), row(2))])
    shown = []
    monkeypatch.setattr(windows.live, "render", lambda state: state["processes"][0]["id"])
    assert windows.watch("unused", 1, reader=lambda root, identifier: next(snapshots),
                         sleeper=lambda seconds: None, output=shown.append) == 0
    assert shown == [1]


def test_terminal_record_with_surviving_child_keeps_viewer():
    frame = view(row(1, status="FINISHED", monitor=False, child=True))
    assert windows.selected(frame, 1) is not None
    assert windows.selected(view(row(1, monitor=False)), 1) is None


def test_busy_database_does_not_close_window(monkeypatch):
    snapshots = iter([view(status="unavailable"), view(row(1)), view()])
    pauses = []
    monkeypatch.setattr(windows.live, "render", lambda state: "frame")
    assert windows.watch("unused", 1, reader=lambda root, identifier: next(snapshots),
                         sleeper=pauses.append, output=lambda text: None) == 0
    assert len(pauses) == 2


def test_creation_uses_disposable_console_and_literal_arguments(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(windows.os, "name", "nt")
    monkeypatch.setattr(windows, "load_project", lambda root: SimpleNamespace(raw={"visible_windows": True}))
    # os.name changes pathlib dispatch; preserve the already-created concrete path.
    monkeypatch.setattr(windows, "Path", type(tmp_path))
    monkeypatch.setattr(windows.subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    assert windows.spawn(tmp_path, 7)
    argv, kwargs = launched[0]
    assert argv[-2:] == [str(tmp_path.resolve()), "7"]
    assert argv[1:3] == ["-m", "agentkit.windows"]
    assert "-NoExit" not in argv
    assert kwargs["creationflags"] == getattr(windows.subprocess, "CREATE_NEW_CONSOLE", 0)
    assert "shell" not in kwargs


def test_disabled_viewer_never_launches(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "load_project", lambda root: SimpleNamespace(raw={"visible_windows": False}))
    monkeypatch.setattr(windows.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("launched")))
    assert not windows.spawn(tmp_path, 1)


def test_exact_viewer_is_not_hidden_by_snapshot_row_limit(project_root, conn, monkeypatch):
    for identifier in (1, 2):
        conn.execute("INSERT INTO processes(id,purpose,provider,status,pid,child_pid,launch_json,started_at) "
                     "VALUES(?, 'worker', 'codex', 'RUNNING', ?, ?, '{}', ?)",
                     (identifier, os.getpid(), os.getpid(), db.utcnow()))
    monkeypatch.setattr(windows.live, "MAX_ROWS", 1)
    assert windows.live.snapshot(project_root)["processes"][0]["id"] == 2
    frame = windows.read_snapshot(project_root, 1)
    assert windows.selected(frame, 1) is not None
    assert [p["id"] for p in frame["processes"]] == [1]


def test_windows_console_receives_frames_even_when_parent_redirects_stdout(monkeypatch, tmp_path):
    frames = []
    @contextmanager
    def console(path, *args, **kwargs):
        assert path == "CONOUT$"
        stream = io.StringIO()
        yield stream
        frames.append(stream.getvalue())
    def watching(*args, **kwargs):
        kwargs["output"]("Visible process activity")
        return 0
    kernel = SimpleNamespace(SetConsoleTitleW=lambda title: None, SetConsoleOutputCP=lambda page: None)
    monkeypatch.setattr(windows.os, "name", "nt")
    monkeypatch.setattr(windows, "Path", type(tmp_path))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(windows, "open", console, raising=False)
    monkeypatch.setattr(windows, "watch", watching)
    assert windows.main([str(tmp_path), "1"]) == 0
    assert frames == ["Visible process activity\n"]


def test_orphan_spawn_retains_uncertain_owner_viewer(project_root, conn, monkeypatch):
    monkeypatch.setattr(windows.live, "pid_alive", lambda pid: False)
    conn.execute("INSERT INTO processes(id,purpose,provider,status,pid,child_pid,launch_json,started_at,child_launch_state) "
                 "VALUES(1,'worker','codex','RUNNING',12345,NULL,'{}',?,'SPAWNING')", (db.utcnow(),))
    frame = windows.read_snapshot(project_root, 1)
    orphan = frame["processes"][0]
    assert orphan["monitor_alive"] is False and orphan["child_alive"] is None
    assert orphan["ownership_uncertain"]
    assert not orphan["liveness_risk"]  # Separate from a confirmed live terminal PID.
    assert windows.selected(frame, 1) is not None
    assert "ownership_uncertain=True" in windows.live.render(frame)


def test_unavailable_runtime_reports_reason_once_instead_of_blank_console():
    unavailable = {**view(status="unavailable"), "message": "Runtime state is busy or incompatible."}
    snapshots = iter([unavailable, unavailable, view()])
    shown = []
    assert windows.watch("unused", 1, reader=lambda root, identifier: next(snapshots),
                         sleeper=lambda seconds: None, output=shown.append) == 0
    assert len(shown) == 1
    assert "busy or incompatible" in shown[0]
    assert "retrying" in shown[0].lower()


def test_verified_model_does_not_hide_requested_effort_in_console_title(monkeypatch):
    active = {**row(1), "model": "claude-opus-5-5", "model_verified": True,
              "requested_effort": "high", "observed_effort": "unknown", "effort": "unknown"}
    snapshots = iter([view(active), view()])
    titles = []
    monkeypatch.setattr(windows.live, "render", lambda state: "frame")
    assert windows.watch("unused", 1, reader=lambda root, identifier: next(snapshots),
                         sleeper=lambda seconds: None, output=lambda frame: None,
                         title=titles.append) == 0
    assert "requested high" in titles[0]
    assert "reported unknown" in titles[0]
