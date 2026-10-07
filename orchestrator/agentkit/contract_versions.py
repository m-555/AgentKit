"""Permit a newer operational lock only when the entire frozen shape is identical."""
from . import contracts


def compatible(task, project, integration_root):
    pinned = task.get("contract_version")
    if pinned is None:
        return True
    current = contracts.load_lock(integration_root)
    original = contracts.load_lock(task["worktree"])
    if (current is None or original is None or original.version != pinned
            or current.version < pinned or original.paths != current.paths):
        return False
    if current.version != pinned and not original.paths:
        return False  # Empty snapshots cannot prove cross-version compatibility.
    # Equal recorded hashes alone do not prove the checked-out bytes/file sets.
    for root in (task["worktree"], integration_root):
        if set(contracts.contract_files(root, project)) != set(original.paths):
            return False
        if contracts.verify_unchanged(root, project):
            return False
    return True
