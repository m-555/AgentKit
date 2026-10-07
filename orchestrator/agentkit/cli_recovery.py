"""Operator visibility/cancellation for generic recovery; never a second agent launcher."""
from __future__ import annotations

import json
import os

from . import db, recovery_store, wake_adapters


def run(args, root):
    conn = db.connect(root)
    try:
        if args.recovery_command == "cancel":
            if os.environ.get("AGENTKIT_TASK") or os.environ.get("AGENTKIT_PROCESS"):
                raise PermissionError("Only the operator can cancel recovery intents")
            result = recovery_store.cancel(conn, args.intent, args.reason)
        elif args.recovery_command == "capabilities":
            result = [value.capability() for value in wake_adapters.REGISTRY.values()]
        else:
            result = recovery_store.snapshot(conn)
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        conn.close()


def configure(sub, handler):
    parser = sub.add_parser("recovery", help="shared CLI/editor recovery states and wake capabilities")
    commands = parser.add_subparsers(dest="recovery_command", required=True)
    for name in ("status", "capabilities", "cancel"):
        command = commands.add_parser(name)
        command.set_defaults(func=handler)
        if name == "cancel":
            command.add_argument("intent")
            command.add_argument("--reason", required=True)
