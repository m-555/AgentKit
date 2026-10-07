"""Codex adapter: scoped hooks require exact-definition trust and live probing.

Codex 0.159.2 documents PreToolUse command payloads for apply_patch and Bash.
Generated project hooks do not establish trust or measured confinement. Never
bypass hook trust or mark L3/L4 active merely because settings were written.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from .. import errors as _errors
from ..capabilities import CapabilitySet
from .base import AgentEvent, BaseAdapter, GuardReport, Installation, Launch

#: Hook events recovered from the binary. Retained for probe use; not depended on.
KNOWN_HOOK_EVENTS = (
    "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
    "SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "SubagentStop",
    "Stop", "Interrupt",
)


def _hook_command(executable: str, *, shell_kind: str | None = None) -> str:
    """Quote Python and preserve the hook's native exit status.

    The currently installed Windows Codex environment selects PowerShell for
    local hooks. This default is an assumption requiring measured certification
    for each runtime; it does not certify a configured cmd.exe environment.
    """
    executable = executable.replace("\\", "/")
    if any(value in executable for value in ('"', "$", "`", "%", "\n", "\r")):
        raise ValueError("Python executable cannot be safely quoted in a hook command")
    base = f'"{executable}" -I -m agentkit.hooks_cli codex-pre-tool-use'
    kind = shell_kind or ("powershell" if os.name == "nt" else "posix")
    if kind == "powershell":
        return f"& {base}; exit $LASTEXITCODE"
    if kind == "posix":
        return base
    raise ValueError("Unverified Codex hook shell; measured shell selection is required")


class CodexAdapter(BaseAdapter):
    name = "codex"

    def check_availability(self) -> dict:
        from .availability import codex_windows, rpc
        install = self.detect()
        if not install:
            return {"available": None, "reason": "Codex is not installed", "windows": []}
        try:
            windows = codex_windows(rpc(install.path, "account/rateLimits/read"))
            return {"available": all(w["used_percent"] < 100 for w in windows) if windows else None,
                    "windows": windows, "complete": bool(windows), "reason": "account/rateLimits/read"}
        except Exception as exc:
            return {"available": None, "windows": [], "reason": str(exc)}

    #: Codex/ChatGPT plan wording. Marked approximate: unlike the Claude strings
    #: these were not observed from a live limited session, so the generic
    #: USAGE_LIMIT patterns remain the real safety net.
    ERROR_PATTERNS = (
        (_errors.USAGE_LIMIT, "Codex plan allowance exhausted", re.compile(
            r"plan\s+limit\s+reached"
            r"|you(?:'ve| have)\s+hit\s+your\s+(?:usage|plan)\s+limit"
            r"|rate\s+limit\s+exceeded\s+for\s+your\s+plan"
            r"|quota\s+exceeded"
            r"|usage\s+limit\.?\s+resets?\s+(?:in|at)",
            re.IGNORECASE)),
        (_errors.AUTH_ERROR, "Codex credentials rejected", re.compile(
            r"codex\s+login|not\s+authenticated|session\s+expired",
            re.IGNORECASE)),
    )

    def detect(self) -> Installation | None:
        found = self._on_path("codex")
        source = "path"
        if not found:
            found = self._find_in_extensions(
                "openai.chatgpt-*", ("bin/*/codex.exe", "bin/*/codex")
            )
            source = "extension"
        if not found:
            return None
        raw = self._run_version(found, ["--version"])
        version = raw.split()[-1] if raw else "unknown"
        return Installation(name=self.name, path=found, version=version, source=source)

    def default_capabilities(self) -> CapabilitySet:
        caps = CapabilitySet(adapter=self.name)
        for name in (
            "prewrite_file_guard", "shell_guard", "workspace_sandbox",
            "resume_session", "fork_session", "structured_output", "mcp_stdio",
            "event_stream", "subagents",
        ):
            caps.set(name, True)
        caps.set("worktree_native", False,
                 "feature flag 'worktrees' is experimental and disabled by default")
        caps.set("budget_cap", False, "no per-run spend cap flag on this build")
        caps.set("network_control", False, "not exposed as a per-run setting")
        return caps

    def feature_flags(self) -> dict[str, tuple[str, bool]]:
        """`codex features list` → {name: (stage, enabled)}. A real probe source."""
        install = self.detect()
        if not install:
            return {}
        import subprocess

        try:
            proc = subprocess.run(
                [install.path, "features", "list"], capture_output=True, text=True,
                timeout=60, errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        flags: dict[str, tuple[str, bool]] = {}
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-1] in ("true", "false"):
                name = parts[0]
                enabled = parts[-1] == "true"
                stage = " ".join(parts[1:-1])
                flags[name] = (stage, enabled)
        return flags

    # -- launching ---------------------------------------------------------

    def build_launch(
        self, task: dict[str, Any], worktree: Path, role: str, project: Any,
        *, prompt: str, resume_token: str | None = None,
    ) -> Launch:
        from .. import workflow
        resume_token = workflow.resume_token(project, {"session_token": resume_token}, True)
        install = self.detect()
        argv: list[str] = [install.path if install else "codex", "exec"]
        if resume_token:
            argv += ["resume", resume_token]
        readonly = role in ("reviewer", "coordinator", "architect", "researcher") or task.get("kind") in ("RESEARCH", "REVIEW")
        # Default workspace-write also allows shared temporary directories. Exclude
        # them and replace inherited extra roots on fresh and resumed launches alike;
        # only the assigned worktree may be writable. Read-only roles stay read-only.
        argv += ["--json", "-c", f'sandbox_mode="{"read-only" if readonly else "workspace-write"}"',
                 "-c", 'approval_policy="never"',
                 "-c", 'sandbox_workspace_write.writable_roots=[]',
                 "-c", 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                 "-c", 'sandbox_workspace_write.exclude_slash_tmp=true',
                 "-c", f"mcp_servers.agentkit.command={json.dumps(sys.executable)}",
                 "-c", 'mcp_servers.agentkit.args=["-m","agentkit.mcp_server"]',
                 "-c", 'mcp_servers.agentkit.env_vars=["AGENTKIT_ROOT","AGENTKIT_TASK","AGENTKIT_GENERATION","AGENTKIT_PROCESS","AGENTKIT_ROLE","AGENTKIT_JOB"]',
                 "-c", 'mcp_servers.agentkit.required=true']
        from ..codex_mcp_policy import approval_flags
        argv += approval_flags(role, readonly=readonly)
        if workflow.enabled(project):
            argv += ["-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false"]
        if not resume_token:
            argv += ["--cd", str(worktree)]
        from ..models import for_launch
        selected = for_launch(project, task, self.name, role)
        argv += ["--model", selected.model, "-c", f'model_reasoning_effort="{selected.effort}"']
        if not readonly and install:
            from ..codex_trust import reviewed_flags
            argv += reviewed_flags(install.path, worktree, _hook_command(sys.executable))
        argv.append("-")
        return Launch(
            argv=argv,
            env={
                "AGENTKIT_TASK": str(task.get("id")),
                "AGENTKIT_GENERATION": str(task.get("generation", 1)),
                "AGENTKIT_WORKTREE": str(worktree.resolve()),
                "AGENTKIT_CODEX_READONLY": "1" if readonly else "0",
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
        """Generate scoped rules and hook definitions without granting hook trust."""
        report = GuardReport()
        rules_dir = worktree / ".codex"
        rules_dir.mkdir(parents=True, exist_ok=True)
        target = rules_dir / "project.rules"

        lines = [
            "# Generated by AgentKit. Codex execpolicy for this worktree.",
            'prefix_rule(pattern=["git", "status"], decision="allow")',
            'prefix_rule(pattern=["git", "diff"], decision="allow")',
            'prefix_rule(pattern=["git", "log"], decision="allow")',
            'prefix_rule(pattern=["git", "add"], decision="allow")',
            'prefix_rule(pattern=["git", "commit"], decision="allow")',
            'prefix_rule(pattern=["git", "push"], decision="deny")',
            'prefix_rule(pattern=["git", "merge"], decision="deny")',
            'prefix_rule(pattern=["git", "rebase"], decision="deny")',
            'prefix_rule(pattern=["git", "reset", "--hard"], decision="deny")',
            'prefix_rule(pattern=["git", "clean", "-xfd"], decision="deny")',
        ]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report.files_written.append(str(target))

        hooks_path = rules_dir / "hooks.json"
        existing = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.is_file() else {}
        if not isinstance(existing, dict) or not isinstance(existing.setdefault("hooks", {}), dict):
            raise ValueError("Refusing to replace malformed existing Codex hooks")
        groups = existing["hooks"].setdefault("PreToolUse", [])
        if not isinstance(groups, list):
            raise ValueError("Refusing to replace malformed existing PreToolUse hooks")
        group = {"matcher": "^(apply_patch|Bash)$", "hooks": [{"type": "command",
                 "command": _hook_command(sys.executable), "timeout": 20}]}
        if group not in groups:
            groups.append(group)
        hooks_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        report.files_written.append(str(hooks_path))
        from ..guard_files import exclude_untracked
        exclude_untracked(worktree, ".codex/project.rules", ".codex/hooks.json")

        report.active.update({"L0_worktree", "L1_sandbox"})
        report.inactive["L2_permissions"] = "project.rules loading is not verified; rely on measured confinement and merge audit"
        trust_pending = (
            "Scoped PreToolUse hooks generated; trusted project layer and exact hook-definition "
            "hash review via /hooks is required; metadata-only trust does not prove exec enforcement; "
            "broad --dangerously-bypass-hook-trust is not used. Windows assumes PowerShell; actual shell execution and functional enforcement remain unverified."
        )
        report.inactive["L3_prewrite_guard"] = trust_pending
        report.inactive["L4_shell_guard"] = trust_pending
        return report

    # -- events ------------------------------------------------------------

    def parse_events(self, stream: IO[str]) -> Iterator[AgentEvent]:
        """Normalise `codex exec --json` JSONL."""
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                yield AgentEvent(kind="text", raw=line)
                continue
            kind = str(msg.get("type") or msg.get("msg", {}).get("type") or "")
            # Codex reports model/effort only in some event shapes; absent means
            # unverified, never "assume the requested model ran".
            inner = msg.get("msg") if isinstance(msg.get("msg"), dict) else {}
            reported = {"model": msg.get("model") or inner.get("model"),
                        "effort": msg.get("reasoning_effort") or inner.get("reasoning_effort")}
            if kind == "thread.started":
                yield AgentEvent(kind="started", detail={"session_id": msg.get("thread_id"), **reported}, raw=line)
            elif kind == "item.completed" and (msg.get("item") or {}).get("type") == "agent_message":
                yield AgentEvent(kind="answer", detail={"text": msg["item"].get("text", "")}, raw=line)
            elif kind == "turn.failed":
                yield AgentEvent(kind="error", detail={"message": msg.get("error")}, raw=line)
            elif "session" in kind and "config" in kind:
                yield AgentEvent(kind="started", detail={"session_id": msg.get("session_id") or inner.get("session_id"),
                                                         **reported}, raw=line)
            elif "command" in kind or "exec" in kind or "patch" in kind:
                yield AgentEvent(kind="tool_use", detail={"codex_type": kind}, raw=line)
            elif "task_complete" in kind or ("turn" in kind and "complete" in kind):
                yield AgentEvent(kind="finished", detail={"codex_type": kind}, raw=line)
            elif "error" in kind:
                yield AgentEvent(kind="error", detail={"codex_type": kind}, raw=line)
            else:
                yield AgentEvent(kind=kind or "unknown", raw=line)
