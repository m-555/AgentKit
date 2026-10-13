"""Explicit test remediation holds an approved commit; never launches a model."""
import json

from . import db, processes, repo, reviews


def dependencies(conn, task, identifiers, coupled=False):
    """Valid testers in the same job. An independent tester must not depend on the
    held source; a coupled one (F38) must, because its tests describe that source."""
    if not identifiers or len(identifiers) > 8 or len(set(identifiers)) != len(identifiers):
        raise ValueError("Remediation requires 1-8 distinct tester tasks")
    result = []
    for identifier in identifiers:
        other = db.get_task(conn, identifier)
        if (not other or identifier == task['id'] or other.get('job_id') != task.get('job_id')
                or not str(other['role']).endswith('-tester') or other['kind'] != 'TEST_ONLY'
                or other['status'] in ('FAILED', 'STALE', 'NEEDS_REPLAN', 'CANCELLED')
                or _depends_on(other, task) != coupled):
            raise ValueError("Remediation must be valid testers in the same job; an independent "
                             "tester must not depend on the held source and a coupled one must")
        result.append(other)
    return result


def _depends_on(other, task):
    return any(str(d) in (str(task['id']), str(task.get('spec_id'))) for d in other['depends_on'])


def hold(conn, task, identifiers, head, evidence, coupled=False):
    dependencies(conn, task, identifiers, coupled)
    meta = {'after_tasks': identifiers, 'head': head, 'evidence': evidence}
    if coupled:
        meta['coupled'] = True
    db.update_task(conn, task['id'], blocked_meta=json.dumps({'integration_remediation': meta}),
        next_action='Approved commit held until explicit tester remediation is integrated.')
    db.log_event(conn, task['id'], 'integration_remediation_held', cause=evidence,
                 detail={'after_tasks': identifiers, 'head': head, 'coupled': coupled, 'model_launch': False})


def held_source(conn, task):
    """The held source a coupled tester builds on, as (source task, approved head), or None."""
    for dependency in task.get('depends_on') or []:
        key = str(dependency)
        row = conn.execute("SELECT id FROM tasks WHERE status='FAILED' AND (spec_id=? OR CAST(id AS TEXT)=?)",
                           (key, key)).fetchone()
        source = db.get_task(conn, row['id']) if row else None
        meta = metadata(source) if source else None
        if (meta and meta.get('coupled') and task.get('id') in meta.get('after_tasks', [])
                and str(source.get('blocker') or '').startswith('combined_gate:')):
            return source, meta['head']
    return None


def merge_problem(conn, project, task, target):
    """Why this branch may not merge, or None (F38). A base outside integration would
    bring commits no audit covered, unless it is a held source's approved head."""
    base = str(task.get('base_sha') or '')
    if not base or repo.is_ancestor(project.root, base, target):
        return None
    held = held_source(conn, task)
    if held is None or held[1] != base:
        return "base commit is not in integration and is not the approved head of a held coupled source"
    if not addressed(conn, project, held[0]):
        return f"held source task {held[0]['id']} changed or lost its approval; its commit cannot be carried"
    return None


def addressed(conn, project, task):
    meta = metadata(task)
    if (not meta or task['status'] != 'FAILED'
            or not str(task.get('blocker') or '').startswith('combined_gate:')):
        return False
    try:
        dependencies(conn, task, meta['after_tasks'], meta.get('coupled', False))
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
        if all(t['status'] == 'DONE' for t in dependencies(conn, task, meta['after_tasks'],
                                                           meta.get('coupled', False))):
            notes.append(authorize(conn, project, task['id'], meta['evidence']))
    return notes


def metadata(task):
    try:
        value = json.loads(task.get('blocked_meta') or '{}')
        return value.get('integration_remediation') if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None
