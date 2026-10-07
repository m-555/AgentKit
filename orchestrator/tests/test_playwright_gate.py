"""Preparation isolates the server without changing a project's test assertions."""
from types import SimpleNamespace

import pytest

from agentkit import gates, playwright_gate, verification


def recipe(tmp_path):
    work = tmp_path / 'worker'
    config = work / 'web/playwright.config.js'
    config.parent.mkdir(parents=True)
    config.write_text('export default { testDir: "./e2e", retries: 0 };')
    project = SimpleNamespace(root=tmp_path, raw={'playwright_checks': [{
        'match_command': 'test:e2e', 'config': 'web/playwright.config.js',
        'server_command': 'npm run dev -- --host 127.0.0.1 --port {port} --strictPort'}]})
    return project, work


def test_private_config_serves_assigned_checkout_and_refuses_reuse(tmp_path):
    project, work = recipe(tmp_path)
    with playwright_gate.prepared(project, work, 'npm run test:e2e -- a.spec.js') as command:
        assert command.startswith('npm run test:e2e -- a.spec.js --config ')
        target = next((tmp_path / '.ai/runtime/browser-checks').glob('*/playwright.config.mjs'))
        text = target.read_text()
        assert (work / 'web/playwright.config.js').resolve().as_uri() in text
        assert 'testDir: resolve(directory, original.testDir' in text
        assert 'reuseExistingServer: false' in text
        assert 'cwd: directory' in text and '--strictPort' in text
        assert 'outputFolder: htmlOutput' in text and 'html-report' in text
        assert 'projects: original.projects?.map' in text and 'use: { ...project.use, baseURL }' in text
        assert '127.0.0.1:5173' not in text
        assert (work / 'web/playwright.config.js').read_text().endswith('retries: 0 };')


def test_non_browser_and_disabled_preparation_preserve_command(tmp_path):
    project, work = recipe(tmp_path)
    with playwright_gate.prepared(project, work, 'pytest') as command:
        assert command == 'pytest'
    project.raw = {}
    with playwright_gate.prepared(project, work, 'npm run test:e2e') as command:
        assert command == 'npm run test:e2e'
    assert not (tmp_path / '.ai').exists()


@pytest.mark.parametrize('command', ['npm run test:e2e && echo pass', 'npm run test:e2e --config other.js'])
def test_ambiguous_commands_fail_before_tests(tmp_path, command):
    project, work = recipe(tmp_path)
    with pytest.raises(ValueError, match='single command'):
        with playwright_gate.prepared(project, work, command):
            pytest.fail('No tests should launch')


def test_escaping_config_fails_before_tests(tmp_path):
    project, work = recipe(tmp_path)
    (tmp_path / 'other.js').write_text('export default {}')
    project.raw['playwright_checks'][0]['config'] = '../other.js'
    with pytest.raises(ValueError, match='assigned checkout'):
        with playwright_gate.prepared(project, work, 'npm run test:e2e'):
            pytest.fail('No tests should launch')


def test_gate_uses_prepared_command(tmp_path, monkeypatch):
    project, work = recipe(tmp_path)
    project.gate = lambda _level: ['npm run test:e2e -- a.spec.js']
    called = []
    def run(command, cwd, timeout):
        called.append((command, cwd))
        return gates.CommandResult(command, 0, 0, '')
    monkeypatch.setattr(gates, '_run_one', run)
    assert gates.run_gate(project, 'browser', cwd=work).passed
    assert '--config ' in called[0][0] and called[0][1] == work


def test_invalid_preparation_is_a_failed_gate_not_a_launch(tmp_path, monkeypatch):
    project, work = recipe(tmp_path)
    project.gate = lambda _level: ['npm run test:e2e --config other.js']
    monkeypatch.setattr(gates, '_run_one', lambda *args: pytest.fail('Must refuse before launch'))
    result = gates.run_gate(project, 'browser', cwd=work)
    assert not result.passed and 'Browser preparation' in result.summary()


def test_browser_recipe_changes_invalidate_verification_cache(project_root, monkeypatch):
    from agentkit.config import load_project
    project = load_project(project_root)
    monkeypatch.setattr(verification.worktrees, 'environment_fingerprint', lambda _root: 'same')
    before = verification.signature(project, project_root, 'fast')
    project.raw['playwright_checks'] = [{'match_command': 'test:e2e', 'config': 'web/playwright.config.js'}]
    assert verification.signature(project, project_root, 'fast') != before
