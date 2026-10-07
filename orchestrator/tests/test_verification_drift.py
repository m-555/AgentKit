"""Preserve the failed check and changed paths when a gate dirties its checkout."""
import pytest

from agentkit import gates, verification


@pytest.mark.parametrize("passed", [True, False])
def test_checkout_drift_keeps_underlying_command_evidence(conn, project, project_root, monkeypatch, passed):
    def run(project, level, **kwargs):
        (project_root / "services/media.py").write_text("VALUE = 'gate mutation'\n")
        command = gates.CommandResult("original check", 0 if passed else 1, 1, "original failure detail")
        return gates.GateResult(level, passed, [command])
    monkeypatch.setattr(gates, "run_gate", run)
    result = verification.run(conn, project, project_root, "full")
    assert not result.passed
    assert len(result.results) == 2
    assert result.results[0].command == "original check"
    assert "services/media.py" in result.summary()
    if not passed:
        assert "original failure detail" in result.summary()
    saved = conn.execute("SELECT passed,summary FROM verification_cache WHERE level='full'").fetchone()
    assert saved[0] == 0
    assert "services/media.py" in saved[1]
