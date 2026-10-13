"""Authorize task checks without applying worker restrictions to managers."""
from __future__ import annotations

import os

from .context import active_task_id
from .workflow import validate_gate


def validate_requested_gate(project, task, level, require_planner):
    if active_task_id() is not None or os.environ.get("AGENTKIT_ROLE") == "review":
        validate_gate(project, task, level)
    else:
        # Extra checks belong to an authorized manager, not an unassigned worker.
        require_planner(task.get("job_id"))


def require_gate_owner(conn, task_id):
    """Revalidate the same kind of owner before execution and receipt recording."""
    from . import processes, worker
    if os.environ.get("AGENTKIT_ROLE") == "review":
        return processes.require_control(conn, purposes=("review",), task_id=task_id)
    return worker.require_current(conn, task_id)
