"""The repository list keeps the most recent use first even when the clock does not move."""
from __future__ import annotations

from datetime import UTC, datetime

from agentkit import project_registry
from tests.test_view_home import agentkit_repo


class FrozenClock:
    """Windows clocks advance in ~15 ms ticks; registrations inside one tick share a time."""

    @staticmethod
    def now(tz=None):
        return datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    fromisoformat = staticmethod(datetime.fromisoformat)


def test_the_latest_registration_comes_first_within_one_clock_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(project_registry, "datetime", FrozenClock)
    zebra = agentkit_repo(tmp_path / "zebra")
    alpha = agentkit_repo(tmp_path / "alpha")
    project_registry.register(alpha)
    project_registry.register(zebra)
    assert [row["name"] for row in project_registry.projects()] == ["zebra", "alpha"]
    project_registry.register(alpha)
    assert [row["name"] for row in project_registry.projects()] == ["alpha", "zebra"]
