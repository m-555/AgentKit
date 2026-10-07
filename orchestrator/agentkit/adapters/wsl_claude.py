"""Opt-in Windows-host / Linux-Claude adapter; vendor/account identity stays Claude."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from ..runtime_identity import enforcement_digest
from ..wsl_runtime import WslConfig
from .base import Installation, Launch
from .claude_code import ClaudeCodeAdapter


def drive_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    if not re.match(r"^[A-Za-z]:/", value):
        raise ValueError("WSL transport requires a local drive worktree; UNC roots are unsupported")
    return "/mnt/" + value[0].lower() + value[2:]


class WslClaudeAdapter(ClaudeCodeAdapter):
    def __init__(self, settings):
        self.config = WslConfig(**{k: v for k, v in settings.items() if k != "type"})

    def query(self, args):
        command = self.config.host_argv()[:8] + args
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def detect(self):
        script = ("import hashlib,json,subprocess,sys; p=sys.argv[1]; "
                  "print(json.dumps({'version':subprocess.check_output([p,'--version'],text=True).split()[0],"
                  "'sha256':hashlib.sha256(open(p,'rb').read()).hexdigest()}))")
        try:
            result = self.query([self.config.python, "-I", "-c", script, self.config.claude])
            measured = json.loads(result.stdout)
            identity = {"path": self.config.claude, "source": f"wsl:{self.config.distro}:{self.config.user}",
                        "sha256": measured["sha256"], "enforcement": enforcement_digest(),
                        "transport": hashlib.sha256(json.dumps(self.config.__dict__, sort_keys=True).encode()).hexdigest()}
            return Installation(self.name, self.config.claude, measured["version"], identity["source"], identity)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return None

    def runtime_problem(self, model):
        from ..discovery import install_problem
        found = self.detect()
        return install_problem(found, model) if found else "Configured WSL Linux Claude is unavailable"

    def probe_path(self, path):
        return drive_path(path)

    def help_text(self, args):
        result = self.query([self.config.claude, *args, "--help"])
        return result.stdout + result.stderr

    def check_availability(self, *, model: str | None = None):
        from ..models import PROFILES
        from ..sessions import model_matches
        from .availability import claude_windows
        model = model or PROFILES["opus"].model
        try:
            proc = self.query([self.config.claude, "-p", "Reply exactly AGENTKIT_AVAILABLE.",
                "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--output-format", "stream-json", "--verbose", "--max-turns", "1", "--model", model])
            events = []
            for line in proc.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            events = [event for event in events if isinstance(event, dict)]
            observed = next((event["model"] for event in events if event.get("model")), None)
            confirmed = proc.returncode == 0 and model_matches(model, observed) and any(
                event.get("type") == "result" and not event.get("is_error") and
                "AGENTKIT_AVAILABLE" in str(event.get("result", "")) for event in events)
            failure = self.classify_error(proc.stdout + "\n" + proc.stderr, proc.returncode)
            return {"available": True if confirmed else False if failure.is_provider_problem else None,
                    "windows": [window for event in events for window in claude_windows(event)],
                    "complete": False, "requested_model": model, "observed_model": observed,
                    "reason": "bounded WSL availability request" if confirmed else failure.reason,
                    "retry_at": failure.retry_at.isoformat() if failure.retry_at else None,
                    "auth_error": failure.kind == "AUTH_ERROR"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": None, "reason": str(exc), "windows": []}

    def build_launch(self, task, worktree, role, project, *, prompt, resume_token=None):
        native = super().build_launch(task, worktree, role, project, prompt=prompt, resume_token=resume_token)
        config = self.config
        index = native.argv.index("--mcp-config") + 1
        native.argv[index] = json.dumps({"mcpServers": {"agentkit": {"command": config.python,
                           "args": ["-I", "-m", "agentkit.wsl_client", "mcp"]}}})
        envelope = {"config": config.__dict__, "argv": native.argv, "cwd": drive_path(worktree),
                    "sha256": self.detect().identity["sha256"]}
        native.env["AGENTKIT_WORKTREE"] = str(worktree.resolve())
        native.env["AGENTKIT_TRANSPORT"] = "wsl"
        return Launch([sys.executable, "-I", "-m", "agentkit.wsl_host", json.dumps(envelope)],
                      native.env, str(worktree), prompt + "\nUse host MCP task_commit and gate_run for Git commits and checks. Linux never opens tasks.db. Do not run Git under WSL.")

    def install_guards(self, worktree, task, orchestrator):
        report = super().install_guards(worktree, task, orchestrator)
        path = worktree / ".claude/settings.local.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for _event, groups in data.get("hooks", {}).items():
            for group in groups:
                for hook in group.get("hooks", []):
                    command = hook.get("command", "")
                    if "agentkit.hooks_cli" in command:
                        name = command.rsplit(" ", 1)[-1]
                        hook["command"] = f'"{self.config.python}" -I -m agentkit.wsl_client hook {name}'
                        hook.pop("async", None)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report.inactive["L1_sandbox"] = "Linux sandbox and full transport require current functional qualification"
        return report
