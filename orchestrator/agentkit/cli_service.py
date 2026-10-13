"""`agentkit service`: the background supervisor's real state, start, stop, reload and autostart."""
from __future__ import annotations

import json


def run(args, root) -> int:
    from . import service, service_control
    command = args.service_command
    if command == "status":
        data = service_control.status(root)
        print(json.dumps(data, indent=2))
        return 0 if data["state"] != "stopped" or not data["stale"] else 1
    if command == "start":
        from .project_registry import register
        register(root)
        print(service.start(root, args.max_workers))
    elif command == "stop":
        print(json.dumps(service_control.stop(root), indent=2))
    elif command == "reload":
        print(json.dumps(service_control.reload(root), indent=2))
    elif args.remove:
        print("Autostart removed." if service_control.remove_autostart(root) else "No autostart entry existed.")
    else:
        print(f"Autostart entry: {service_control.install_autostart(root)}")
    return 0


def configure(sub, root_or_exit) -> None:
    parser = sub.add_parser("service", help="background supervisor: status, start, stop, reload, autostart")
    commands = parser.add_subparsers(dest="service_command", required=True)
    for name, text in (("status", "is the guardian and supervisor really running (not just the health file)"),
                       ("start", "start the guardian unless a verified one already runs"),
                       ("stop", "stop the verified guardian, then its supervisor; workers keep running"),
                       ("reload", "restart only the supervisor so it loads current AgentKit code"),
                       ("autostart", "start this project's supervisor at Windows logon")):
        command = commands.add_parser(name, help=text)
        command.set_defaults(func=lambda args: run(args, root_or_exit(args.path)))
        if name == "start":
            command.add_argument("--max-workers", type=int, default=None)
        elif name == "autostart":
            command.add_argument("--remove", action="store_true", help="remove the logon entry instead")
    from .cli_view import configure as configure_view  # the other background service: the view
    configure_view(sub, root_or_exit)
