"""A failed combined gate waits for accepted tests without replacement builders."""
import pytest

from agentkit import db, integration_remediation, integration_retry, repo
from tests.conftest import commit_all
from tests.test_integration_retry import failed


def setup(conn, project, project_root):
    identifier, work, head = failed(conn, project, project_root)
    db.update_task(conn, identifier, job_id='repair-job')
    tester = db.create_task(conn, title='compatibility', role='backend-tester',
                            kind='TEST_ONLY', status='READY', job_id='repair-job')
    return identifier, work, head, tester


def test_hold_waits_for_done_tester_then_queues_unchanged_commit(conn, project, project_root):
    identifier, work, head, tester = setup(conn, project, project_root)
    integration_retry.authorize(conn, project, identifier, 'Explicit compatible-test remediation', [tester])
    task = db.get_task(conn, identifier)
    assert task['status'] == 'FAILED'
    assert integration_remediation.addressed(conn, project, task)
    assert integration_remediation.release_ready(conn, project) == []
    conn.execute("UPDATE tasks SET status='DONE' WHERE id=?", (tester,))
    assert integration_remediation.release_ready(conn, project)
    assert db.get_task(conn, identifier)['status'] == 'INTEGRATION_READY'
    assert repo.head_commit(work) == head
    assert conn.execute('SELECT count(*) FROM processes').fetchone()[0] == 0


@pytest.mark.parametrize('problem', ['dependent', 'foreign', 'builder', 'failed', 'changed_source'])
def test_invalid_or_changed_remediation_is_not_an_addressed_failure(conn, project, project_root, problem):
    identifier, work, _, tester = setup(conn, project, project_root)
    integration_retry.authorize(conn, project, identifier, 'Bounded test repair', [tester])
    if problem == 'dependent':
        db.update_task(conn, tester, depends_on=[identifier])
    elif problem == 'foreign':
        db.update_task(conn, tester, job_id='different-job')
    elif problem == 'builder':
        db.update_task(conn, tester, role='backend-builder')
    elif problem == 'failed':
        conn.execute("UPDATE tasks SET status='FAILED' WHERE id=?", (tester,))
    else:
        (work / 'services/retry.py').write_text('UNREVIEWED = True\n')
        commit_all(work, 'unreviewed change')
    assert not integration_remediation.addressed(conn, project, db.get_task(conn, identifier))
    assert integration_remediation.release_ready(conn, project) == []
    assert db.get_task(conn, identifier)['status'] == 'FAILED'
