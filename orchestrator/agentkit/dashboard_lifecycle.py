"""Read-only waiting reasons and recorded continuation metadata for the view."""
from __future__ import annotations

import json
import sqlite3

from . import db


def enrich(root, view):
    try:
        conn = db.connect_readonly(root)
    except (OSError, sqlite3.Error):
        return
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs = {row['id']: row['status'] for row in conn.execute('SELECT id,status FROM jobs LIMIT 200')} if 'jobs' in tables else {}
        columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
        fields = [key for key in ('job_id', 'depends_on', 'blocker', 'next_action') if key in columns]
        tasks = {row['id']: dict(row) for row in conn.execute('SELECT id,' + ','.join(fields) + ' FROM tasks ORDER BY id DESC LIMIT 200')} if fields else {}
        statuses = {task.get('spec_id'): task.get('status') for task in view.get('tasks', [])}
        for task in view.get('tasks', []):
            metadata = tasks.get(task['id'], {})
            task['job_state'] = jobs.get(metadata.get('job_id'))
            task['blocker'] = str(db.redact(metadata.get('blocker') or ''))[:600]
            task['next_action'] = str(db.redact(metadata.get('next_action') or ''))[:600]
            try:
                dependencies = json.loads(metadata.get('depends_on') or '[]')
            except (ValueError, TypeError):
                dependencies = []
            task['dependencies'] = [{'id': name, 'status': statuses.get(name, 'unknown')}
                                    for name in dependencies[:32] if isinstance(name, str)] if isinstance(dependencies, list) else []
        if 'handoffs' in tables:
            records = [dict(row) for row in conn.execute('SELECT task_id,generation,source_provider,source_model,target_provider,target_model,created_at FROM handoffs ORDER BY id DESC LIMIT 200')]
            for process in view.get('processes', []):
                record = next((record for record in records if record['task_id'] == process.get('task_id')
                               and record['generation'] == (process.get('generation') or 0) + 1
                               and record['source_provider'] == process.get('provider')
                               and record['source_model'] == (process.get('observed_model') if process.get('model_verified') else process.get('requested_model'))
                               and (record['source_provider'], record['source_model']) != (record['target_provider'], record['target_model'])), None)
                if record:
                    process['continuation'] = db.redact(record)
    except (OSError, sqlite3.Error):
        pass  # Old runtime schemas remain readable; unavailable detail is unknown.
    finally:
        conn.close()
