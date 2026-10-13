"""`agentkit providers hold`: the user stops a provider until they release it with `providers clear`."""
from __future__ import annotations


def run(args, root) -> int:
    from . import db, providers
    conn = db.connect(root)
    try:
        state = providers.hold(conn, args.provider, reason=args.reason, account=args.account)
    finally:
        conn.close()
    print(f"{state.provider} HELD: no new launches or coordinator wakes on it until "
          f"`agentkit providers clear {state.provider}`.")
    return 0


def configure(commands, root_or_exit) -> None:
    command = commands.add_parser("hold", help="stop a provider until the user clears it; nothing lifts it automatically")
    command.add_argument("provider")
    command.add_argument("--account", default="default")
    command.add_argument("--reason", required=True, help="why; shown in status and the view")
    command.set_defaults(func=lambda args: run(args, root_or_exit(args.path)))
