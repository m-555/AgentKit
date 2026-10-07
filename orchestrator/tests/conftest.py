"""Fixtures for the acceptance suite.

These tests exercise *core* guarantees — the layers that hold for every agent,
including ones with no hook system. They never launch a real agent: adapter-level
L3/L4 behaviour is proven separately by `agentkit probe --functional`, which is
the only honest way to test it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentkit import db
from agentkit.config import load_project
from agentkit.init_project import init


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=120
    )


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small, realistic repository: two services, a router, tests and a contract."""
    root = tmp_path / "demo"
    (root / "services").mkdir(parents=True)
    (root / "routes").mkdir()
    (root / "tests").mkdir()
    (root / "contracts").mkdir()

    (root / "services" / "media.py").write_text("VALUE = 'media'\n", encoding="utf-8")
    (root / "services" / "retry.py").write_text("VALUE = 'retry'\n", encoding="utf-8")
    (root / "routes" / "video.py").write_text("ROUTES = []\n", encoding="utf-8")
    (root / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    (root / "contracts" / "api.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@agentkit.local")
    git(root, "config", "user.name", "agentkit-test")
    git(root, "config", "commit.gpgsign", "false")
    commit_all(root, "initial")
    return root


@pytest.fixture()
def project_root(repo: Path) -> Path:
    """A repository that has been onboarded, with real gate commands."""
    init(repo)
    config = repo / ".ai" / "project.yaml"
    config.write_text(
        "name: demo\n"
        "stacks: [python]\n"
        "gates:\n"
        '  fast: ["python -c \\"print(1)\\""]\n'
        '  full: ["python -c \\"print(1)\\""]\n'
        "hot_paths:\n"
        "  - routes/video.py\n"
        "contracts:\n"
        "  - contracts/**\n",
        encoding="utf-8",
    )
    commit_all(repo, "onboard")
    return repo


@pytest.fixture()
def conn(project_root: Path):
    connection = db.connect(project_root)
    yield connection
    connection.close()


@pytest.fixture()
def project(project_root: Path):
    return load_project(project_root)


@pytest.fixture()
def make_task(conn):
    """Create a task in a live state, holding the given paths."""
    def _make(title: str, paths: list[str], *, kind: str = "SAFE_PARALLEL",
              status: str = "RUNNING", spec_id: str | None = None, **extra):
        return db.create_task(
            conn, title=title, spec_id=spec_id or title.lower().replace(" ", "-"),
            owned_paths=paths, expected_write=paths, kind=kind, status=status, **extra,
        )
    return _make
