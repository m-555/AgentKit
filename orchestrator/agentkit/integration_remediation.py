"""Explicit test remediation holds an approved commit; never launches a model."""
import json

from . import db, processes, repo, reviews


def dependencies(conn, task, identifiers):
    if not identifiers or len(identifiers) > 8 or len(set(identifiers)) != len(identifiers):
        raise ValueError("Remediation requires 1-8 distinct tester tasks")
    result = []
    for identifier in identifiers:
        other = db.get_task(conn, identifier)
        if (not other or identifier == task['id'] or other.get('job_id') != task.get('job_id')
                or not str(other['role']).endswith('-tester') or other['kind'] != 'TEST_ONLY'
                or other['status'] in ('FAILED', 'STALE', 'NEEDS_REPLAN', 'CANCELLED')
                or any(str(d) in (str(task['id']), str(task.get('spec_id')))
                       for d in other['depends_on'])):
            raise ValueError("Remediation must be independent valid testers in the same job")
        result.append(other)
    return result


def hold(conn, task, identifiers, head, evidence):
    dependencies(conn, task, identifiers)
    db.update_task(conn, task['id'], blocked_meta=json.dumps({'integration_remediation': {
        'after_tasks': identifiers, 'head': head, 'evidence': evidence}}),
        next_action='Approved commit held until explicit tester remediation is integrated.')
    db.log_event(conn, task['id'], 'integration_remediation_held', cause=evidence,
                 detail={'after_tasks': identifiers, 'head': head, 'model_launch': False})


def addressed(conn, project, task):
    meta = metadata(task)
    if (not meta or task['status'] != 'FAILED'
            or not str(task.get('blocker') or '').startswith('combined_gate:')):
        return False
    try:
        dependencies(conn, task, meta['after_tasks'])
        if any(p['task_id'] == task['id'] for p in processes.owning(conn)):
            return False
        return (repo.is_clean(task['worktree']) and
                reviews.require_approval(conn, task, project) == meta['head'])
    except (KeyError, OSError, ValueError):
        return False


def release_ready(conn, project):
    from .integration_retry import authorize
    notes = []
    for task in db.list_tasks(conn, ('FAILED',)):
        if not addressed(conn, project, task):
            continue
        meta = metadata(task)
        assert meta is not None
        if all(t['status'] == 'DONE' for t in dependencies(conn, task, meta['after_tasks'])):
            notes.append(authorize(conn, project, task['id'], meta['evidence']))
    return notes


def metadata(task):
    try:
        value = json.loads(task.get('blocked_meta') or '{}')
        return value.get('integration_remediation') if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None
