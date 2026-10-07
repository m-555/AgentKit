"""The ownership decision.

This is the function standing between two agents and a corrupted merge, so the
tests here are about *policy*, not plumbing: who gets blocked, who does not, and
whether the reason given is actionable.
"""

import pytest

from agentkit import db
from agentkit.config import ProjectConfig
from agentkit.leases import decide


@pytest.fixture()
def project(tmp_path):
    (tmp_path / ".ai").mkdir()
    return ProjectConfig(
        root=tmp_path,
        name="demo",
        hot_paths=["routes/video.py"],
        contracts=["contracts/**"],
    )


@pytest.fixture()
def conn(tmp_path, project):
    connection = db.connect(tmp_path)
    yield connection
    connection.close()


def _task(conn, title, paths, status="RUNNING"):
    task_id = db.create_task(conn, title=title, owned_paths=paths, status=status)
    return task_id


class TestOwnScope:
    def test_inside_scope_is_allowed(self, conn, project):
        task = _task(conn, "retry", ["services/retry.py"])
        assert decide(conn, project, "services/retry.py", task).allowed

    def test_outside_scope_is_blocked(self, conn, project):
        task = _task(conn, "retry", ["services/retry.py"])
        verdict = decide(conn, project, "services/other.py", task)
        assert not verdict.allowed
        assert verdict.code == "out_of_scope"

    def test_block_reason_names_the_scope(self, conn, project):
        """A blocked agent must be able to act on the message without guessing."""
        task = _task(conn, "retry", ["services/retry.py"])
        verdict = decide(conn, project, "services/other.py", task)
        assert "services/retry.py" in verdict.reason
        assert "lease_request" in verdict.reason

    def test_glob_scope_is_honoured(self, conn, project):
        task = _task(conn, "media", ["services/media/**"])
        assert decide(conn, project, "services/media/providers/veo.py", task).allowed


class TestForeignClaims:
    def test_another_live_task_blocks(self, conn, project):
        owner = _task(conn, "split media", ["services/media.py"])
        intruder = _task(conn, "retry", ["services/retry.py"])
        verdict = decide(conn, project, "services/media.py", intruder)
        assert not verdict.allowed
        assert verdict.code == "owned_by_other"
        assert verdict.owner_task == owner

    def test_finished_task_does_not_block(self, conn, project):
        """DONE tasks release their claim, otherwise the graph would deadlock."""
        _task(conn, "old work", ["services/media.py"], status="DONE")
        intruder = _task(conn, "new work", ["services/media.py"])
        assert decide(conn, project, "services/media.py", intruder).allowed

    def test_explicit_lease_blocks_even_without_owned_paths(self, conn, project):
        owner = _task(conn, "hotspot", [])
        db.acquire_leases(conn, owner, ["services/media.py"])
        intruder = _task(conn, "other", ["services/retry.py"])
        verdict = decide(conn, project, "services/media.py", intruder)
        assert not verdict.allowed
        assert verdict.owner_task == owner

    def test_released_lease_stops_blocking(self, conn, project):
        owner = _task(conn, "hotspot", [])
        db.acquire_leases(conn, owner, ["services/media.py"])
        db.release_leases(conn, owner)
        intruder = _task(conn, "other", [])
        assert decide(conn, project, "services/media.py", intruder).allowed


class TestProtectedPaths:
    def test_hot_path_needs_explicit_ownership(self, conn, project):
        task = _task(conn, "unscoped", [])
        verdict = decide(conn, project, "routes/video.py", task)
        assert not verdict.allowed
        assert verdict.code == "protected_path"

    def test_hot_path_allowed_when_explicitly_owned(self, conn, project):
        task = _task(conn, "split router", ["routes/video.py"])
        assert decide(conn, project, "routes/video.py", task).allowed

    def test_contract_paths_are_protected(self, conn, project):
        task = _task(conn, "unscoped", [])
        verdict = decide(conn, project, "contracts/api.yaml", task)
        assert not verdict.allowed
        assert "contract" in verdict.reason

    def test_framework_state_is_never_editable(self, conn, project):
        task = _task(conn, "anything", [".ai/**"])
        verdict = decide(conn, project, ".ai/tasks.db", task)
        assert not verdict.allowed
        assert verdict.code == "protected_state"


class TestPermissiveDefaults:
    """Installing the plugin must not break ordinary sessions.

    CHANGED (§3): this class previously asserted that *any* session without a
    task could write anywhere, including in a managed repository. That was the
    bypass — a manually opened agent session could ignore every active lease
    simply by having no task id. The permissive path now applies only to
    repositories AgentKit does not manage; see `TestManagedRepositoryDenies`.
    """

    def test_unmanaged_repository_is_left_alone(self, conn, project):
        """No `.ai/project.yaml` here, so this repo was never onboarded."""
        verdict = decide(conn, project, "anything/at/all.py", None)
        assert verdict.allowed
        assert verdict.code == "unmanaged_repository"

    def test_unknown_task_does_not_block(self, conn, project):
        assert decide(conn, project, "services/x.py", 9999).allowed

    def test_unscoped_task_allows_ordinary_paths(self, conn, project):
        task = _task(conn, "not yet planned", [])
        verdict = decide(conn, project, "services/anything.py", task)
        assert verdict.allowed
        assert verdict.code == "unscoped_task"

    def test_empty_path_is_allowed(self, conn, project):
        task = _task(conn, "x", ["services/**"])
        assert decide(conn, project, "", task).allowed
