"""Read-only browser visibility for existing AgentKit sessions."""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from . import live
from .secrets import redact_text

TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "thinking_tokens",
    "cached_input_tokens", "cache_write_input_tokens",
)
CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _text(value: object, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in redact_text(value) if char.isprintable())[:limit]


def unknown_usage() -> dict:
    return dict.fromkeys(TOKEN_FIELDS) | {"source": "unavailable", "complete": False}


def read_usage(root: str | Path, process_id: int) -> dict:
    """Isolate missing or unreadable usage; never request usage from a provider."""
    try:
        from .session_usage import read_details as reader
        usage = reader(Path(root), process_id)
    except Exception:
        return unknown_usage()
    if not isinstance(usage, dict):
        return unknown_usage()
    clean = unknown_usage()
    for key in TOKEN_FIELDS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            clean[key] = value
    turns = usage.get("agent_turns")
    clean["agent_turns"] = turns if type(turns) is int and turns >= 0 else None
    clean["counter_scope"] = "session_total" if usage.get("counter_scope") == "session_total" else "observed_events"
    clean["stream_complete"] = usage.get("stream_complete") is True
    clean["source"] = _text(usage.get("source"), 120) or "unavailable"
    clean["complete"] = usage.get("complete") is True
    return clean


def _paths(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        paths = json.loads(value)
    except (ValueError, RecursionError):
        return []
    if not isinstance(paths, list):
        return []
    return [_text(path, 300) for path in paths[:128] if isinstance(path, str)]


def _task_metadata(root: Path, task_ids: set[int]) -> dict[int, dict]:
    """Supplement safe task metadata missing from live.snapshot, without migrations."""
    if not task_ids:
        return {}
    path = root / ".ai" / "tasks.db"
    conn = None
    try:
        path = path.resolve()
        path.relative_to(root.resolve())
        if not path.is_file():
            return {}
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        fields = [key for key in ("description", "expected_write", "expected_read")
                  if key in columns]
        if not fields:
            return {}
        selected = ",".join(
            f"substr({key},1,{600 if key == 'description' else 32768}) AS {key}"
            for key in fields
        )
        rows = conn.execute(
            f"SELECT id,{selected} FROM tasks ORDER BY id DESC LIMIT ?", (live.MAX_ROWS,)
        )
        return {
            row["id"]: {
                key: _text(row[key]) if key == "description" else _paths(row[key])
                for key in fields
            }
            for row in rows if row["id"] in task_ids
        }
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return {}
    finally:
        if conn is not None:
            conn.close()


def build_snapshot(root: str | Path) -> dict:
    """Return recorded state and counters only; no transcript or reasoning content."""
    root = Path(root).resolve()
    view = live.snapshot(root)
    tasks = view.get("tasks", [])
    metadata = _task_metadata(root, {task["id"] for task in tasks})
    for task in tasks:
        task.setdefault("description", "")
        task.setdefault("expected_write", [])
        task.setdefault("expected_read", [])
        task.update(metadata.get(task["id"], {}))
    for process in view.get("processes", []):
        process["usage"] = read_usage(root, process["id"])
    for manager in view.get("external_managers", []):
        manager["usage"] = unknown_usage()
    from .project_usage import snapshot as project_usage
    view["project_usage"] = project_usage(root, {p["id"]: p["usage"] for p in view.get("processes", [])})
    from .config import load_project
    from .quota_report import reports
    project = load_project(root)
    view["execution_paused"] = project.raw.get("execution_paused") is True
    view["workflow_mode"] = _text((project.raw.get("workflow") or {}).get("mode", "legacy"), 80)
    from .review_policy import mode
    view["review_mode"] = mode(project)
    from .dashboard_lifecycle import enrich
    enrich(root, view)
    view["allowance_reports"] = reports(view)
    from . import db, recovery_store, wake_adapters
    from .native_session_registration import snapshot as native_registrations
    view["native_recovery_registrations"] = native_registrations(root)
    view["recovery_intents"] = []
    view["wake_capabilities"] = [a.capability() for a in wake_adapters.REGISTRY.values()]
    if db.db_path(root).is_file():
        recovery_conn = db.connect(root)
        try:
            view["recovery_intents"] = recovery_store.snapshot(recovery_conn)
        finally:
            recovery_conn.close()
    view["project"] = _text(root.name, 120)
    return view


class DashboardServer(ThreadingHTTPServer):
    """An ephemeral, loopback-only server; no scheduler or command execution."""

    daemon_threads = True


def make_server(root: str | Path, port: int = 8765, *, operator_controls=False) -> DashboardServer:
    root = Path(root).resolve()
    from .dashboard_actions import Controls
    controls = Controls(root, operator_controls)
    html = Path(__file__).with_name("dashboard.html").read_bytes()
    assets = {"/" + name: Path(__file__).with_name(name).read_bytes()
              for name in ("dashboard_team.js", "dashboard_view.js", "dashboard_allowance.js", "dashboard_review.js", "dashboard_categories.js", "dashboard_workspaces.js", "dashboard_recovery.js", "dashboard_activity.js")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentKitDashboard"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.wfile.write(body)

        def _local_request(self) -> bool:
            actual_port = cast(tuple[str, int], self.server.server_address)[1]
            hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            if actual_port == 80:
                hosts.update(("127.0.0.1", "localhost"))
            host = self.headers.get("Host", "").lower()
            origin = self.headers.get("Origin")
            return host in hosts and (not origin or origin == f"http://{host}")

        def do_GET(self) -> None:
            if not self._local_request():
                self._send(403, b"Local origin required.\n", "text/plain; charset=utf-8")
                return
            route = urlsplit(self.path).path
            if route == "/":
                self._send(200, html, "text/html; charset=utf-8")
            elif route in assets:
                self._send(200, assets[route], "text/javascript; charset=utf-8")
            elif route == "/api/workspaces":
                from .workspace_view import snapshot
                try:
                    self._send(200, json.dumps(snapshot(root)).encode("utf-8"), "application/json; charset=utf-8")
                except Exception:
                    self._send(503, b'{"message":"Workspace inventory unavailable; preserve state for recovery."}', "application/json; charset=utf-8")
            elif route == "/api/reviews":
                self._send(200, json.dumps(controls.snapshot()).encode("utf-8"), "application/json; charset=utf-8")
            elif route == "/api/snapshot":
                try:
                    body = json.dumps(build_snapshot(root), ensure_ascii=True,
                                      allow_nan=False).encode("utf-8")
                except Exception:
                    self._send(503, b'{"status":"unavailable","message":"Snapshot unavailable."}',
                               "application/json; charset=utf-8")
                    return
                self._send(200, body, "application/json; charset=utf-8")
            elif route.startswith("/api/activity/"):
                from .public_activity import endpoint
                identifier = route.removeprefix("/api/activity/")
                if not identifier.isascii() or not identifier.isdecimal() or not 0 < len(identifier) < 10:
                    self._send(404, b"Not found.\n", "text/plain; charset=utf-8")
                    return
                status, result = endpoint(Path(root), int(identifier))
                self._send(status, json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")
            elif route.startswith("/api/terminal/"):
                from .terminal_mirror import frame
                key = route.removeprefix("/api/terminal/")
                status, body, kind = frame(Path(root), key)
                self._send(status, body, kind)
            else:
                self._send(404, b"Not found.\n", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
            self.do_GET()

        def _reject_mutation(self) -> None:
            self._send(405, b"Read-only dashboard.\n", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            # Drain small bodies even on rejection, so Windows clients receive
            # the HTTP refusal instead of an unread-body connection reset.
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if 0 < length <= 16384 else b""
            except (ValueError, OSError):
                self._send(400, b"Invalid request body.\n", "text/plain; charset=utf-8")
                return
            if not self._local_request():
                self._send(403, b"Local origin required.\n", "text/plain; charset=utf-8")
                return
            self.connection.settimeout(10)
            status, result = controls.post(urlsplit(self.path).path, self.headers, io.BytesIO(body))
            self._send(status, json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")

        do_PUT = _reject_mutation
        do_PATCH = _reject_mutation
        do_DELETE = _reject_mutation
        do_OPTIONS = _reject_mutation

    return DashboardServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Existing project directory")
    parser.add_argument("--port", type=int, default=8765, help="Local HTTP port (default: 8765)")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in your browser")
    parser.add_argument("--operator-controls", action="store_true", help="Enable local human decisions and idle-project profile changes; starts no jobs")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error("--root must be an existing directory")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = (make_server(args.root, args.port, operator_controls=True) if args.operator_controls
                  else make_server(args.root, args.port))
    except OSError as exc:
        parser.exit(1, f"Cannot start dashboard: {exc}\n")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    label = "local operator dashboard" if args.operator_controls else "read-only dashboard"
    print(f"AgentKit {label}: {url}\nPress Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
