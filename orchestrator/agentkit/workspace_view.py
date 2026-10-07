"""Read-only workspace discovery, separate from frequent session polling."""
from . import db, repo, workspace_registry


def snapshot(root):
    conn = db.connect_readonly(root)
    try:
        rows = workspace_registry.inventory(root, db.list_tasks(conn))
        import json

        from .config import load_project
        from .environment_prepare import receipt_path
        project = load_project(root)
        from .integrator import integration_branch
        branch = integration_branch(project)
        checkout = next((entry.get("worktree") for entry in repo.worktree_list(root)
                         if entry.get("branch") == "refs/heads/" + branch), None)
        integration = {"branch": branch, "checkout": checkout,
                       "head": repo.rev_parse(root, branch) if repo.branch_exists(root, branch) else None}
        for row in rows:
            if row.get("path"):
                path = receipt_path(project, row["path"])
                row["environment"] = json.loads(path.read_text()) if path.exists() else {"status": "not_prepared", "ai_calls": 0}
        return {"manifest": str(workspace_registry.manifest(root)), "integration": integration, "workspaces": rows[:256],
                "total": len(rows), "truncated": len(rows) > 256}
    finally:
        conn.close()
