"""`agentkit view` opens the view with one command and keeps it online."""
from __future__ import annotations

from types import SimpleNamespace

from agentkit import cli, cli_view, view_keeper


def _args(**values):
    return SimpleNamespace(**{"port": 8766, "no_open": False, "stop": False, **values})


def test_the_view_is_marked_wanted_ensured_and_opened(project_root):
    opened, ensured = [], []
    code = cli_view.run(_args(), project_root, opener=opened.append,
                        ensure=lambda root, port: ensured.append(port) or "started 4321")
    assert code == 0 and ensured == [8766] and opened == ["http://127.0.0.1:8766/"]
    assert view_keeper.state(project_root) == {"always_on": True, "port": 8766}


def test_another_projects_view_on_the_port_is_reported(project_root):
    assert cli_view.run(_args(no_open=True), project_root, ensure=lambda root, port: "port busy") == 1


def test_stop_clears_the_wish(project_root, monkeypatch):
    view_keeper.save(project_root, {"always_on": True, "port": 8766})
    monkeypatch.setattr(view_keeper, "probe", lambda port: None)
    cli_view.run(_args(stop=True), project_root)
    assert view_keeper.state(project_root)["always_on"] is False


def test_outside_a_repository_the_view_opens_the_last_used_one(project_root, tmp_path, monkeypatch):
    from agentkit import project_registry
    project_registry.register(project_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    never = lambda explicit: (_ for _ in ()).throw(AssertionError("asked for a repository"))  # noqa: E731
    assert cli_view._root(None, never) == project_root.resolve()


def test_the_command_is_registered():
    args = cli.build_parser().parse_args(["view", "--no-open", "--port", "8800"])
    assert args.port == 8800 and args.no_open and not args.stop
