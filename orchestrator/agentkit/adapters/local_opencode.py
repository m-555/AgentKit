"""Bounded, read-only Qwen workers using the existing Local-opencode router."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from ..capabilities import CapabilitySet
from .base import AgentEvent, BaseAdapter, GuardReport, Installation, Launch


def local_config() -> dict:
    """Import only the loopback provider, never the other repo's agent workflow."""
    configured = os.environ.get("AGENTKIT_LOCAL_OPENCODE")
    candidates = [Path(configured)] if configured else [
        Path(__file__).resolve().parents[3].parent / "local-opencode",
        Path.cwd().parent / "local-opencode",
    ]
    source = next((p / "opencode.json" for p in candidates if (p / "opencode.json").is_file()), None)
    if source is None:
        raise ValueError("Set AGENTKIT_LOCAL_OPENCODE to your Local-opencode repository")
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    provider = raw.get("provider", {}).get("local", {})
    options = provider.get("options", {})
    endpoint = urlsplit(options.get("baseURL", ""))
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("Local-opencode must use an HTTP 127.0.0.1 endpoint without credentials or query")
    return {"npm": "@ai-sdk/openai-compatible", "name": "Local Qwen",
            "options": {"baseURL": options["baseURL"], "apiKey": "local-loopback-only",
                        "timeout": 1800000, "chunkTimeout": 600000},
            "models": provider.get("models", {})}


_SECRET_READS = {"*.env": "deny", "*.env.*": "deny", "*.pem": "deny", "*.key": "deny"}


def _declared(task, key):
    value = task.get(key) or []
    if isinstance(value, str):  # an undecoded JSON column
        value = json.loads(value or "[]")
    return [str(item) for item in value]


def read_scope(task):
    """Read permissions for a local worker.

    A task that declares its inputs may read only those files and its own
    declared outputs. glob, grep, list and other directories are denied, because
    they would reveal what reads are denied. Secrets stay denied either way. A
    task with nothing declared keeps the earlier broad read rule.
    """
    declared = [*_declared(task, "expected_read"), *_declared(task, "expected_write")]
    if not declared:
        return {"read": {"*": "allow", **_SECRET_READS}, "glob": "allow", "grep": "allow"}
    read = {"*": "deny"}
    for path in declared:
        clean = path.replace("\\", "/")
        clean = clean[2:] if clean.startswith("./") else clean
        read["*" + clean] = "allow"
        read["*" + clean.replace("/", "\\")] = "allow"
    read.update(_SECRET_READS)
    return {"read": read, "glob": "deny", "grep": "deny", "list": "deny", "external_directory": "deny"}


def standing_rules(task, project):
    """The local worker's system prompt: the rules that must hold for the whole run.

    OpenCode re-sends the agent prompt on every request and never compacts it,
    while the launch message and tool results, the brief included, can be
    summarised away when the context fills.
    """
    from ..commit_format import worker_note
    lines = [f"You are the read-only research worker for AgentKit task {task['id']}: {task.get('title') or ''}",
             "Complete only this task. Do not write files, run commands or delegate. Save findings with "
             "agentkit_checkpoint, then set task_status REVIEW. If a required tool is denied, report BLOCKED."]
    if task.get("description"):
        lines.append("Assignment: " + str(task["description"]))
    lines += [f"Required: {item}" for item in _declared(task, "acceptance")]
    readable = [*_declared(task, "expected_read"), *_declared(task, "expected_write")]
    if readable:
        lines.append("You may read only: " + ", ".join(readable) + ". Other reads are denied; do not retry them.")
    note = worker_note(project)
    if note:
        lines.append(note)
    return "\n".join(lines)


class LocalOpenCodeAdapter(BaseAdapter):
    name = "local-opencode"
    allowance_policy = "unmetered"  # This adapter accepts loopback local models only.
    supports_write_probe = False

    def detect(self):
        found = self._on_path("opencode")
        if not found or Path(found).suffix.lower() in (".cmd", ".ps1", ".bat"):
            binary = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/opencode-ai/bin/opencode.exe"
            found = str(binary) if binary.is_file() else None
        if not found:
            return None
        return Installation(self.name, found, self._run_version(found, ["--version"]) or "unknown")

    def default_capabilities(self):
        caps = CapabilitySet(adapter=self.name)
        for name in ("resume_session", "structured_output", "mcp_stdio", "event_stream"):
            caps.set(name, True)
        caps.notes["workspace_sandbox"] = "tool permissions only; unattended write tasks are disabled"
        caps.notes["structured_output"] = "JSONL transport events; final answer text still requires schema validation"
        return caps

    def check_availability(self):
        try:
            provider = local_config()
            # A router catalog read does not load a model or consume inference.
            opener = build_opener(ProxyHandler({}))
            with opener.open(provider["options"]["baseURL"].rstrip("/") + "/models", timeout=5) as response:
                catalog = json.load(response)
            names = [item["id"] for item in catalog.get("data", []) if isinstance(item, dict) and "id" in item]
            return {"available": bool(names), "complete": True, "windows": [],
                    "models": names, "reason": "local router catalog reachable; inference not tested"}
        except (OSError, ValueError) as exc:
            return {"available": False, "windows": [], "reason":
                    f"Local router unavailable: {exc}. Start Local-opencode/scripts/start-router.ps1, then retry providers check."}

    def build_launch(self, task, worktree, role, project, *, prompt, resume_token=None):
        from ..models import for_launch
        if task.get("kind") != "RESEARCH" or role in ("coordinator", "reviewer"):
            raise ValueError("Local Qwen currently supports easy RESEARCH tasks only; write isolation is unproven")
        selected = for_launch(project, task, self.name, role)
        if selected.name != "qwen" or task.get("complexity") != "easy":
            raise ValueError("Local Qwen requires an explicitly easy task")
        provider = local_config()
        namespace, _, model_id = selected.model.partition("/")
        if namespace != "local" or model_id not in provider["models"]:
            raise ValueError("selected Qwen model is missing from Local-opencode's provider configuration")
        provider["models"] = {model_id: provider["models"][model_id]}
        permissions = {"*": "deny", **read_scope(task),
                       "agentkit_brief": "allow", "agentkit_checkpoint": "allow", "agentkit_task_status": "allow",
                       "agentkit_gate_run": "allow"}
        config = {"$schema": "https://opencode.ai/config.json", "model": selected.model,
                  "small_model": selected.model, "enabled_providers": ["local"], "provider": {"local": provider},
                  "share": "disabled", "autoupdate": False, "permission": permissions,
                  "agent": {"agentkit-worker": {"mode": "primary", "steps": 20, "permission": permissions,
                            "prompt": standing_rules(task, project)}},
                  "mcp": {"agentkit": {"type": "local", "command": [sys.executable, "-m", "agentkit.mcp_server"], "enabled": True}}}
        from .. import workflow
        resume_token = workflow.resume_token(project, {"session_token": resume_token}, True)
        install = self.detect()
        argv = [install.path if install else "opencode", "run", "--pure", "--format", "json",
                "--model", selected.model, "--agent", "agentkit-worker"]
        if resume_token:
            argv += ["--session", resume_token]
        root = project.root if project else worktree
        runtime = root / ".ai/runtime/local-opencode"
        env = {"AGENTKIT_ROOT": str(root), "AGENTKIT_TASK": str(task["id"]),
               "AGENTKIT_GENERATION": str(task.get("generation", 1)), "AGENTKIT_MODEL": selected.model,
               "AGENTKIT_MODEL_PROFILE": selected.name, "AGENTKIT_MODEL_EFFORT": "",
               "OPENCODE_CONFIG_CONTENT": json.dumps(config), "OPENCODE_PERMISSION": json.dumps(permissions),
               "OPENCODE_CONFIG_DIR": str(runtime / "config"), "XDG_CONFIG_HOME": str(runtime / "xdg"),
               "XDG_DATA_HOME": str(runtime / "data"), "XDG_STATE_HOME": str(runtime / "state"),
               "XDG_CACHE_HOME": str(runtime / "cache"),
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1",
               "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}
        return Launch(argv=argv, env=env, cwd=str(worktree), stdin_text=prompt)

    def install_guards(self, worktree, task, orchestrator):
        return GuardReport(active={"L0_worktree", "L2_permissions"},
                           inactive={"L1_sandbox": "No measured OS confinement; read-only tasks only"})

    def parse_events(self, stream):
        session = None
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                if line.strip():
                    yield AgentEvent(kind="text", raw=line.strip())
                continue
            if not isinstance(event, dict):
                continue
            if event.get("sessionID") and event["sessionID"] != session:
                session = event["sessionID"]
                yield AgentEvent(kind="started", detail={"session_id": session}, raw=line)
            kind = event.get("type")
            if kind == "error":
                yield AgentEvent(kind="finished", detail={"is_error": True, "result": event.get("error")}, raw=line)
            elif kind == "step_finish":
                # Tool-use steps are not completion. Runner owns final process exit.
                yield AgentEvent(kind="cost", detail={"cost_usd": (event.get("part") or {}).get("cost", 0)}, raw=line)
            elif kind == "text":
                yield AgentEvent(kind="answer", detail={"text": (event.get("part") or {}).get("text", "")}, raw=line)
            elif kind == "tool_use":
                yield AgentEvent(kind=kind, raw=line)
