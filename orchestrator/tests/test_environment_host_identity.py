"""Switching the AgentKit host venv must not invalidate identical project dependencies."""
from agentkit import environment_profiles as profiles


def test_host_venv_wrappers_share_one_dependency_fingerprint(project_root, monkeypatch):
    profile = {'name': 'static', 'tools': [], 'strategy': 'commands'}
    monkeypatch.setattr(profiles.sys, '_base_executable', 'E:/Python/python.exe')
    monkeypatch.setattr(profiles.sys, 'executable', 'E:/host-a/Scripts/python.exe')
    first = profiles.fingerprint(project_root, profile)
    monkeypatch.setattr(profiles.sys, 'executable', 'E:/host-b/Scripts/python.exe')
    assert profiles.fingerprint(project_root, profile) == first
    monkeypatch.setattr(profiles.sys, '_base_executable', 'E:/DifferentPython/python.exe')
    assert profiles.fingerprint(project_root, profile) != first
