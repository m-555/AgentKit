"""Manager queries remain small even with large unrelated operational histories."""
import json

import pytest

from agentkit import compact_jobs, db, jobs, mcp_server


@pytest.fixture
def state(project_root, conn, monkeypatch):
    monkeypatch.setenv('AGENTKIT_ROOT', str(project_root))
    jobs.create(project_root, 'one', 'Keep every user instruction', 'codex',
                acceptance=['Preserve existing features'])
    jobs.create(project_root, 'other', 'Unrelated job', 'codex')
    for number in range(55):
        db.create_task(conn, title=f'task-{number}', job_id='one',
                       status='DONE' if number < 5 else 'PLANNED',
                       description='operational context ' * 10000)
    db.create_task(conn, title='Never leak cross-job task', job_id='other')
    jobs.amend(project_root, 'one', 'historical evidence ' * 20000)
    return project_root, conn


def test_default_mcp_read_preserves_intent_and_bounds_operational_history(state):
    root, conn = state
    before = conn.total_changes
    raw = mcp_server.job_brief('one')
    result = json.loads(raw)
    assert result['requests'] == jobs.load(root, 'one')['requests']
    assert result['acceptance'] == ['Preserve existing features']
    assert result['unfinished_count'] == 50
    assert result['task_counts'] == {'DONE': 5, 'PLANNED': 50}
    assert len(result['tasks']) == 20 and result['next_offset'] == 20
    assert 'operational context' not in raw and 'Never leak cross-job' not in raw
    assert result['decisions'][0]['omitted_characters'] > 300000
    assert len(raw) < 16000 and conn.total_changes == before


def test_paging_covers_every_unfinished_task_exactly_once(state):
    root, conn = state
    pages = [compact_jobs.summary(conn, root, 'one', offset=n) for n in (0, 20, 40)]
    identifiers = [t['id'] for page in pages for t in page['tasks']]
    assert len(identifiers) == len(set(identifiers)) == 50
    assert pages[-1]['next_offset'] is None


@pytest.mark.parametrize('offset,limit', [(-1, 20), (True, 20), (0, 0), (0, 51), (0, True)])
def test_invalid_pages_refused(state, offset, limit):
    root, conn = state
    with pytest.raises(ValueError):
        compact_jobs.summary(conn, root, 'one', offset=offset, limit=limit)


def test_explicit_history_still_available(state):
    root, conn = state
    raw = mcp_server.job_brief('one', include_history=True)
    assert 'historical evidence ' * 100 in raw
    assert 'operational context ' * 100 in raw


def test_decision_receipt_does_not_repeat_the_entire_job(state):
    root, conn = state
    receipt = json.loads(mcp_server.job_decision('one', 'Choose the narrow next task'))
    assert receipt == {'id': 'one', 'revision': 1, 'decision_count': 2}
    assert jobs.load(root, 'one')['decisions'][-1]['text'] == 'Choose the narrow next task'
