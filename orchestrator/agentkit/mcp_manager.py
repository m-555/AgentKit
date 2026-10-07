"""Manager lifecycle tools registered on the caller's actual MCP server."""
from __future__ import annotations

import json
import os

from . import db, manager, manager_audit
from .mcp_extra import _root


def credential(root, job_id: str) -> str | None:
    from .jobs import path as job_path
    job_path(root, job_id)  # Validate the job slug before resolving a credential file.
    path = root / ".ai" / "runtime" / f"manager-{job_id}.credential"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def manager_audit_run(job_id: str, include_report: bool = False) -> str:
    """Audit recovery and return a compact receipt; full evidence is retained in state.

    Request include_report only when inspecting the detailed saved audit. Normal
    acknowledgements need the digest and findings, not repeated job conversations.
    """
    root = _root()
    conn = db.connect(root)
    try:
        token = None if os.environ.get("AGENTKIT_PROCESS") else credential(root, job_id)
        result = manager_audit.run(conn, root, job_id, token=token)
        if not include_report:
            report = result["report"]
            result = {key: value for key, value in result.items() if key != "report"}
            result["summary"] = {
                "integration_head": report.get("integration", {}).get("head"),
                "task_count": len(report.get("tasks", [])),
                "job_revision": report.get("job", {}).get("revision"),
                "runtime_status": report.get("runtime", {}).get("status"),
            }
            result["evidence_storage"] = "manager_state.audit_report"
        return json.dumps(result, default=str)
    finally:
        conn.close()


def manager_ack(job_id: str, epoch: int, digest: str, evidence: str) -> str:
    """Acknowledge only the current successful, unchanged recovery audit."""
    root = _root()
    conn = db.connect(root)
    try:
        token = None if os.environ.get("AGENTKIT_PROCESS") else credential(root, job_id)
        manager_audit.acknowledge(conn, root, job_id, epoch, digest, evidence, token=token)
        return "Current recovery epoch acknowledged."
    finally:
        conn.close()


def manager_checkpoint(job_id: str, payload: dict) -> str:
    """Save external manager decisions, checks and next action as durable job memory."""
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("external manager checkpoint requires its own lease")
    root = _root()
    conn = db.connect(root)
    try:
        token = credential(root, job_id)
        if not token:
            raise PermissionError("attach external manager with agentkit manager attach first")
        return json.dumps({"checkpoint": manager.checkpoint(conn, job_id, token, payload)})
    finally:
        conn.close()


def register(server):
    server.add_tool(manager_audit_run, name="manager_audit")
    server.add_tool(manager_ack)
    server.add_tool(manager_checkpoint)
