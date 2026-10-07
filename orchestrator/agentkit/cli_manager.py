"""CLI bridge for an external manager, with credentials kept off stdout."""
from __future__ import annotations

import json
from pathlib import Path

from . import db, manager, manager_audit
from .config import load_project
from .locking import atomic_write
from .mcp_manager import credential


def run(args, root) -> int:
    conn = db.connect(root)
    job_id = args.job
    try:
        action = args.manager_command
        if action == "attach":
            from . import jobs, models
            from .external_identity import reference
            from .external_review import enabled
            provider = models.control_candidates(load_project(root), jobs.load(root, job_id), "coordinator")[0].provider
            session_ref = getattr(args, "session_ref", "") or reference(provider)
            if enabled(load_project(root)) and not session_ref.strip():
                raise PermissionError("external-manager attachment requires the selected provider identity (CODEX_THREAD_ID, CLAUDE_CODE_SESSION_ID or local session reference) or --session-ref")
            new_token = manager.attach(conn, root, job_id, args.holder, args.pid,
                                       ttl_seconds=args.ttl, session_ref=session_ref)
            path = root / ".ai" / "runtime" / f"manager-{job_id}.credential"
            atomic_write(path, new_token + "\n")
            print(json.dumps({"job": job_id, "holder": args.holder, "credential_file": str(path)}))
            return 0
        if action == "repin":
            from .manager_repin import repin
            evidence = Path(args.evidence_file).read_text(encoding="utf-8")
            data = repin(conn, root, job_id, args.profile, evidence, effort=args.effort)
            print(json.dumps(data, indent=2, default=str))
            return 0
        token = credential(root, job_id)
        if action == "handover":
            from . import jobs, models
            from .external_identity import reference
            if not token:
                raise PermissionError("external manager is not attached")
            provider = models.control_candidates(load_project(root), jobs.load(root, job_id), "coordinator")[0].provider
            session_ref = getattr(args, "session_ref", "") or reference(provider)
            new_token = manager.handover(conn, job_id, token, args.holder, args.pid,
                                         session_ref=session_ref, ttl_seconds=args.ttl)
            path = root / ".ai" / "runtime" / f"manager-{job_id}.credential"
            atomic_write(path, new_token + "\n")
            print(json.dumps({"job": job_id, "holder": args.holder, "credential_file": str(path)}))
            return 0
        if action == "status":
            data = manager.packet(conn, job_id)
            record = manager.lease(conn, job_id)
            data["lease"] = {k: v for k, v in (record or {}).items() if k != "token_hash"}
        elif action == "audit":
            data = manager_audit.run(conn, root, job_id, token=token)
        elif action == "review":
            from .external_review import submit
            task = db.get_task(conn, args.task)
            if not task or task.get("job_id") != job_id:
                raise PermissionError("review task belongs to another job")
            submit(conn, load_project(root), args.task, args.head, args.verdict,
                   Path(args.evidence_file).read_text(encoding="utf-8"))
            data = {"job": job_id, "task": args.task, "reviewed_head": args.head,
                    "verdict": args.verdict}
        elif action == "ack":
            manager_audit.acknowledge(conn, root, job_id, args.epoch, args.digest, Path(args.evidence_file).read_text(encoding="utf-8"), token=token)
            data = {"job": job_id, "acknowledged_epoch": args.epoch}
        else:
            if not token:
                raise PermissionError("external manager is not attached")
            if action == "heartbeat":
                manager.heartbeat(conn, job_id, token)
                data = {"job": job_id, "heartbeat": "recorded"}
            elif action == "release":
                manager.release(conn, job_id, token)
                data = {"job": job_id, "released": True}
            else:
                payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
                data = {"job": job_id, "checkpoint": manager.checkpoint(conn, job_id, token, payload)}
        print(json.dumps(data, indent=2, default=str))
        return 0
    finally:
        conn.close()


def configure(sub, handler) -> None:
    parser = sub.add_parser("manager", help="external manager bridge and recovery audit")
    commands = parser.add_subparsers(dest="manager_command", required=True)
    for name in ("attach", "heartbeat", "checkpoint", "release", "status", "audit", "ack", "review", "repin",
                 "handover"):
        command = commands.add_parser(name)
        command.add_argument("job")
        command.set_defaults(func=handler)
        if name == "repin":
            from .models import CONTROL
            command.add_argument("--profile", required=True, choices=CONTROL,
                                 help="control profile the user appointed as manager")
            command.add_argument("--effort", default=None, help="override the profile's configured effort")
            command.add_argument("--evidence-file", required=True,
                                 help="the user's decision and why, recorded in the job")
        elif name in ("attach", "handover"):
            command.add_argument("--holder", required=True)
            command.add_argument("--session-ref", default="", help="Registered external provider session identity")
            command.add_argument("--pid", type=int, required=True, help="PID of the persistent external bridge, not this one-shot command")
            command.add_argument("--ttl", type=int, default=90)
        elif name == "checkpoint":
            command.add_argument("--file", required=True)
        elif name == "ack":
            command.add_argument("--epoch", type=int, required=True)
            command.add_argument("--digest", required=True)
            command.add_argument("--evidence-file", required=True)
        elif name == "review":
            command.add_argument("--task", type=int, required=True)
            command.add_argument("--head", required=True)
            command.add_argument("--verdict", choices=("PASS", "CHANGES", "REJECT"), required=True)
            command.add_argument("--evidence-file", required=True)
