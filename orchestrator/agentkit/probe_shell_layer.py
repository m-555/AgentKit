"""Separate a fixture's OS boundary measurement from its command lease guard."""
from __future__ import annotations

import json
from contextlib import contextmanager


@contextmanager
def measure_os_boundary(adapter_name, workdir, *, launch=None):
    # Only the generated disposable fixture changes. Real worker hooks stay intact.
    if adapter_name == "codex" and launch is not None:
        argv = list(launch.argv)
        path = workdir / ".codex/hooks.json"
        original = path.read_bytes() if path.exists() else None
        try:
            if original is not None:
                path.unlink()
            index = 0
            filtered = []
            while index < len(argv):
                if argv[index] == "-c" and index + 1 < len(argv) and argv[index + 1].startswith(("hooks.PreToolUse=", "hooks.state=")):
                    index += 2
                else:
                    filtered.append(argv[index])
                    index += 1
            launch.argv = filtered
            yield
        finally:
            launch.argv = argv
            if original is not None:
                path.write_bytes(original)
        return
    path = workdir / ".claude" / "settings.local.json"
    if adapter_name != "claude-code" or not path.is_file():
        yield
        return
    original = path.read_bytes()
    settings = json.loads(original)
    groups = settings.get("hooks", {}).get("PreToolUse", [])
    retained = []
    for group in groups:
        hooks = [hook for hook in group.get("hooks", [])
                 if not any(name in hook.get("command", "") for name in ("agentkit.hooks_cli pre-bash", "agentkit.wsl_client hook pre-bash"))]
        if hooks:
            retained.append({**group, "hooks": hooks})
    settings.setdefault("hooks", {})["PreToolUse"] = retained
    try:
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        yield
    finally:
        path.write_bytes(original)
