"""Claude Code adapter.

Capability grades verified on 2.1.167 CLI / 2.1.270 VS Code extension â€” see
PLAN_V3 Â§0.1. Nothing here is imported by core; the registry hands core an
object satisfying `AgentAdapter` and core never asks which one it got.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from .. import errors as _errors
from ..capabilities import CapabilitySet
from .base import AgentEvent, BaseAdapter, GuardReport, Installation, Launch


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude-code"
    availability_requires_inference = True

    def check_availability(self, *, model: str | None = None) -> dict:
        import subprocess
        import tempfile

        from ..models import PROFILES
        from ..secrets import worker_environment
        from ..sessions import model_matches
        from .availability import claude_windows
        model = model or PROFILES["opus"].model
        install = self.detect()
        if not install:
            return {"available": None, "reason": "Claude is not installed", "windows": []}
        # No undocumented credential extraction or private HTTP endpoint. This
        # bounded, tool-free request can consume a small amount of allowance.
        try:
            with tempfile.TemporaryDirectory(prefix="agentkit-availability-") as work:
                proc = subprocess.run([install.path, "-p", "Reply exactly AGENTKIT_AVAILABLE.",
                    "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--output-format", "stream-json", "--verbose", "--max-turns", "1", "--model", model],
                    cwd=work, env=worker_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=45,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            windows = []
            confirmed = False
            observed_model = None
            for line in proc.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                windows.extend(claude_windows(event))
                if event.get("model"):
                    observed_model = event["model"]
                if event.get("type") == "result" and not event.get("is_error"):
                    confirmed = "AGENTKIT_AVAILABLE" in str(event.get("result", ""))
            confirmed = confirmed and model_matches(model, observed_model)
            classification = self.classify_error(proc.stderr + "\n" + proc.stdout, proc.returncode)
            return {"available": True if confirmed and proc.returncode == 0 else False if classification.is_provider_problem else None,
                    "windows": windows, "complete": False, "reason": classification.reason,
                    "requested_model": model, "observed_model": observed_model,
                    "retry_at": classification.retry_at.isoformat() if classification.retry_at else None,
                    "auth_error": classification.kind == _errors.AUTH_ERROR}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": None, "reason": str(exc), "windows": []}

    #: Claude Code's own wording, checked before the generic patterns.
    #: "Claude usage limit reached. Your limit will reset at 3pm" matched none of
    #: the previous implementation's markers and fell through to CRASH, which
    #: consumed an attempt â€” three of those pushed a healthy task to NEEDS_REPLAN.
    ERROR_PATTERNS = (
        (_errors.USAGE_LIMIT, "Claude subscription allowance exhausted", re.compile(
            r"claude\s+usage\s+limit\s+reached"
            r"|your\s+limit\s+will\s+reset\s+at"
            r"|approaching\s+(?:your\s+)?usage\s+limit"
            r"|(?:5|five)[- ]hour\s+limit"
            r"|weekly\s+limit\s+reached",
            re.IGNORECASE)),
        (_errors.AUTH_ERROR, "Claude credentials rejected", re.compile(
            r"please\s+run\s+/login|invalid\s+api\s+key"
            r"|oauth\s+token\s+(?:expired|revoked)",
            re.IGNORECASE)),
        (_errors.PROVIDER_OUTAGE, "Anthropic API unavailable", re.compile(
            r"overloaded_error|api_error|internal\s+server\s+error",
            re.IGNORECASE)),
    )

    # -- discovery ---------------------------------------------------------

    def detect(self) -> Installation | None:
        """The highest-version CLI found on PATH or in an editor extension, or the pin."""
        from .. import discovery
        selected = discovery.best(discovery.candidates())
        if selected is None:
            return None
        return Installation(name=self.name, path=selected.path, version=selected.version,
                            source=selected.source)

    def runtime_problem(self, model: str) -> str | None:
        from .. import discovery
        install = self.detect()
        if install is None or not discovery.install_problem(install, model):
            return None
        found = discovery.candidates()
        selected = next((c for c in found if c.path == install.path), None)
        return discovery.problem(found, selected, model) if selected else discovery.install_problem(install, model)

    def default_capabilities(self) -> CapabilitySet:
        caps = CapabilitySet(adapter=self.name)
        for name in (
            "prewrite_file_guard", "shell_guard", "workspace_sandbox", "network_control",
            "resume_session", "fork_session", "structured_output", "mcp_stdio",
            "worktree_native", "budget_cap", "event_stream", "subagents",
        ):
            caps.set(name, True)
        return caps

    # -- launching ---------------------------------------------------------

    def build_launch(
        self, task: dict[str, Any], worktree: Path, role: str, project: Any,
        *, prompt: str, resume_token: str | None = None,
    ) -> Launch:
        from .. import workflow
        resume_token = workflow.resume_token(project, {"session_token": resume_token}, True)
        install = self.detect()
        argv: list[str] = [install.path if install else "claude", "-p"]

        from ..discovery import install_problem
        from ..models import for_launch
        selected = for_launch(project, task, self.name, role)
        issue = install_problem(install, selected.model) if install else None
        if issue:
            raise ValueError(issue)
        argv += ["--model", selected.model]
        if selected.effort:
            argv += ["--effort", selected.effort]
        if resume_token:
            argv += ["--resume", resume_token]
        from ..run_limits import limits
        cap = limits(project).get("max_turns")
        if cap is not None and role not in ("reviewer", "coordinator", "architect", "researcher"):
            argv += ["--max-turns", str(cap)]

        argv += [
            "--permission-mode", "acceptEdits",
            "--output-format", "stream-json",
            "--include-hook-events",
            "--verbose",
        ]
        argv += ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {"agentkit": {
            "command": sys.executable, "args": ["-m", "agentkit.mcp_server"]}}})]
        if role in ("reviewer", "coordinator", "architect", "researcher") or task.get("kind") in ("RESEARCH", "REVIEW"):
            argv += ["--tools", "Read,Grep,Glob,ToolSearch,WaitForMcpServers"]
        if workflow.enabled(project) and task.get("kind") not in ("RESEARCH", "REVIEW") and role not in ("reviewer", "coordinator", "architect", "researcher"):
            argv += ["--tools", "Read,Grep,Glob,Edit,Write,Bash,ToolSearch,WaitForMcpServers"]
        argv += ["--allowedTools", "mcp__agentkit__*"]
        budget = task.get("budget_usd") or (project.budgets.get("worker_usd") if project else None)
        if budget:
            argv += ["--max-budget-usd", str(budget)]

        return Launch(
            argv=argv,
            env={
                "AGENTKIT_TASK": str(task.get("id")),
                "AGENTKIT_GENERATION": str(task.get("generation", 1)),
                "AGENTKIT_ROOT": str(project.root) if project else str(worktree),
                "AGENTKIT_MODEL": selected.model, "AGENTKIT_MODEL_PROFILE": selected.name,
                "AGENTKIT_MODEL_EFFORT": selected.effort or "",
            },
            cwd=str(worktree),
            stdin_text=prompt,
        )

    # -- guards ------------------------------------------------------------

    def install_guards(
        self, worktree: Path, task: dict[str, Any], orchestrator: Path
    ) -> GuardReport:
        """Write worker-scoped settings into the worktree.

        `.claude/settings.local.json` is not committed, which is what we want:
        these settings describe one worker's confinement, not the project's policy.
        """
        report = GuardReport()
        settings_dir = worktree / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        target = settings_dir / "settings.local.json"

        executable = '"' + sys.executable.replace("\\", "/") + '"'
        hook_cmd: dict[str, Any] = {
            "type": "command",
            "command": executable + " -m agentkit.hooks_cli pre-tool-use",
            "timeout": 20,
        }
        audit_cmd = {
            "type": "command",
            "command": executable + " -m agentkit.hooks_cli post-tool-use",
            "timeout": 30,
            "async": True,
        }

        existing: dict[str, Any] = {}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8")) or {}
            except json.JSONDecodeError:
                existing = {}

        existing.setdefault("permissions", {})
        deny = set(existing["permissions"].get("deny", []))
        deny.update([
            "Read(.env)", "Edit(.env)", "Read(.env.*)", "Edit(.env.*)",
            "Read(**/*.pem)", "Read(**/id_rsa*)", "Read(**/.ssh/**)",
            "Edit(.ai/tasks.db)",
            "Bash(git push *)",
            "Bash(git commit * --no-verify*)",
            "Bash(git commit *-n *)",
            "Bash(git reset --hard *)",
            "Bash(git clean -xfd*)",
            "Bash(git merge *)",
            "Bash(git rebase *)",
        ])
        existing["permissions"]["deny"] = sorted(deny)
        existing["permissions"]["defaultMode"] = "acceptEdits"

        hooks = existing.setdefault("hooks", {})
        for event in ("PreToolUse", "PostToolUse"):
            retained = []
            for group in hooks.get(event, []):
                entries = [h for h in group.get("hooks", [])
                           if "agentkit.hooks_cli" not in h.get("command", "")]
                if entries:
                    retained.append({**group, "hooks": entries})
            hooks[event] = retained
        hooks.setdefault("PreToolUse", []).extend([
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [hook_cmd]},
            {"matcher": "Bash|PowerShell", "hooks": [{**hook_cmd, "command": executable + " -m agentkit.hooks_cli pre-bash"}]},
        ])
        hooks.setdefault("PostToolUse", []).extend([
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell", "hooks": [audit_cmd]},
        ])

        target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        report.files_written.append(str(target))
        report.active.update({"L0_worktree", "L2_permissions",
                              "L3_prewrite_guard", "L4_shell_guard"})
        # The OS sandbox is a user/managed-settings concern, not writable per worktree.
        report.inactive["L1_sandbox"] = (
            "sandbox.enabled is a user-level setting; enable it in ~/.claude/settings.json"
        )
        return report

    # -- events ------------------------------------------------------------

    def parse_events(self, stream: IO[str]) -> Iterator[AgentEvent]:
        """Normalise `--output-format stream-json` lines."""
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                yield AgentEvent(kind="text", raw=line)
                continue
            kind = str(msg.get("type") or "")
            if kind == "rate_limit_event":
                from .availability import claude_windows
                yield AgentEvent(kind="quota", detail={"windows": claude_windows(msg)}, raw=line)
                continue
            if kind == "system" and msg.get("subtype") == "init":
                # `model` is the provider's own report, so it can verify the pin.
                yield AgentEvent(kind="started", detail={"session_id": msg.get("session_id"),
                                                         "model": msg.get("model")}, raw=line)
            elif kind == "assistant":
                yield AgentEvent(kind="tool_use" if _has_tool_use(msg) else "text", raw=line)
            elif kind == "result":
                yield AgentEvent(
                    kind="finished",
                    detail={
                        "is_error": bool(msg.get("is_error")),
                        "cost_usd": msg.get("total_cost_usd"),
                        "session_id": msg.get("session_id"),
                        "result": msg.get("result"),
                        "model": msg.get("model"), "effort": msg.get("effort"),
                        "models_used": sorted(msg.get("modelUsage") or {}),
                    },
                    raw=line,
                )
            else:
                yield AgentEvent(kind=kind or "unknown", raw=line)


def _has_tool_use(msg: dict[str, Any]) -> bool:
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    return False
