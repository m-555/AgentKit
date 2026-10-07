"""Small manager reads; durable user intent stays complete and history stays available."""
from __future__ import annotations

from collections import Counter

from . import db, jobs, manager_state


def _page(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a non-negative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('limit must be between 1 and 50')


def _preview(value, maximum=400):
    text = str(value or '')
    return {'text': text[:maximum], 'characters': len(text),
            'omitted_characters': max(0, len(text) - maximum)}


def summary(conn, root, job_id: str, *, offset=0, limit=20) -> dict:
    """Read a page of unfinished work, never nested recovery/audit/checkpoint history."""
    _page(offset, limit)
    job = jobs.load(root, job_id)
    tasks = [t for t in db.list_tasks(conn) if t.get('job_id') == job_id]
    pending = [t for t in tasks if t['status'] not in ('DONE', 'CANCELLED')]
    pending.sort(key=lambda t: t['id'])
    runtime = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    recovery = manager_state.state(conn, job_id)
    lease = conn.execute('SELECT holder,provider,model,effort,heartbeat_at,released_at '
                         'FROM manager_leases WHERE job_id=?', (job_id,)).fetchone()
    decisions = job.get('decisions') or []
    rows = []
    for task in pending[offset:offset + limit]:
        rows.append({**{key: task.get(key) for key in (
            'id', 'spec_id', 'title', 'role', 'status', 'depends_on', 'expected_write')},
            'blocker': _preview(task.get('blocker'))})
    return {
        'id': job['id'], 'revision': job['revision'],
        'requests': job.get('requests') or [],  # User intent is never truncated.
        'acceptance': job.get('acceptance') or [],
        'decisions': [{'at': item.get('at'), **_preview(item.get('text'))}
                      for item in decisions[-5:]],
        'decision_count': len(decisions),
        'task_counts': dict(Counter(t['status'] for t in tasks)),
        'tasks': rows, 'unfinished_count': len(pending),
        'offset': offset, 'limit': limit,
        'next_offset': offset + limit if offset + limit < len(pending) else None,
        'runtime': {key: runtime[key] for key in (
            'status', 'revision', 'planned_revision', 'completed_sha')} if runtime else None,
        'manager': dict(lease) if lease else None,
        'recovery': {key: recovery.get(key) for key in (
            'epoch', 'acknowledged_epoch', 'audit_epoch', 'audit_digest')},
        'history': 'Use job_brief(include_history=True) for complete decisions and runtime evidence.',
    }
