"""Explicit profile conversion never discards started work or activates a job."""
from __future__ import annotations

import pytest

from agentkit import db, jobs, workflow_setup
from agentkit.config import load_project
from tests.test_workflow_controls import profile


def test_convert_unstarted_job_requires_new_plan_and_forces_pause(project_root, conn):
    original = jobs.create(project_root, "feature", "Build a feature")
    db.create_task(conn, title="old plan", job_id="feature", status="READY")
    result = workflow_setup.apply(project_root, profile(), replan_unstarted="feature")
    assert result['execution_paused'] is True
    assert load_project(project_root).raw['workflow']['review'] == 'human'
    job = jobs.load(project_root, 'feature')
    assert job['revision'] == original['revision'] + 1 and job['workflow_review'] == 'human'
    state = conn.execute("SELECT * FROM jobs WHERE id='feature'").fetchone()
    assert state['status'] == 'PLANNING' and state['planned_revision'] == 0
    assert conn.execute('SELECT count(*) FROM processes').fetchone()[0] == 0


@pytest.mark.parametrize('blocker', ['active_job', 'worker_history', 'started_task', 'other_job', 'lease'])
def test_convert_refuses_prior_work_and_other_jobs_without_changing_intent(project_root, conn, blocker):
    jobs.create(project_root, 'feature', 'Keep existing intent')
    task = db.create_task(conn, title='existing', job_id='feature')
    if blocker == 'active_job':
        conn.execute("UPDATE jobs SET status='ACTIVE' WHERE id='feature'")
    elif blocker == 'worker_history':
        conn.execute("INSERT INTO processes(purpose,job_id,provider,status,launch_json,started_at) VALUES('worker','feature','codex','FINISHED','{}',?)", (db.utcnow(),))
    elif blocker == 'started_task':
        db.update_task(conn, task, generation=1)
    elif blocker == 'other_job':
        jobs.create(project_root, 'another', 'Another job')
    else:
        conn.execute("INSERT INTO leases(task_id,path_glob,mode,acquired_at) VALUES(?,?,'exclusive-write',?)", (task, 'source.py', db.utcnow()))
    project_before = (project_root / '.ai/project.yaml').read_bytes()
    job_before = jobs.path(project_root, 'feature').read_bytes()
    with pytest.raises(ValueError):
        workflow_setup.apply(project_root, profile(), replan_unstarted='feature')
    assert (project_root / '.ai/project.yaml').read_bytes() == project_before
    assert jobs.path(project_root, 'feature').read_bytes() == job_before
