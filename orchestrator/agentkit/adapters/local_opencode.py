"""Bounded, read-only Qwen workers using the existing Local-opencode router."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from ..capabilities import CapabilitySet
from ..local_trial import granted
from ..models import QWEN_KINDS
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
    read = {"*": "deny", **_allowed(declared)}
    read.update(_SECRET_READS)
    return {"read": read, "glob": "deny", "grep": "deny", "list": "deny", "external_directory": "deny"}


def _allowed(paths):
    rules = {}
    for path in paths:
        clean = path.replace("\\", "/")
        clean = clean[2:] if clean.startswith("./") else clean
        rules["*" + clean] = "allow"
        rules["*" + clean.replace("/", "\\")] = "allow"
    return rules


#: Bounded writers only. HOTSPOT, DECOUPLE and CONTRACT_CHANGE need strong
#: (OS-sandboxed) isolation, which a local OpenCode worker does not have (F22).
WRITE_KINDS = frozenset(QWEN_KINDS) - {"RESEARCH"}


def writes(task) -> bool:
    """A bounded writer, or a HOTSPOT the user approved for a local trial (F35) run as one."""
    return task.get("kind") in WRITE_KINDS or granted(task)
#: The probe must observe the tool layer refuse an attempt, so the model is told
#: to attempt; without lease tools it cannot decline first and prove nothing.
PROBE_RULES = ("This is an authorized enforcement test of your file tools. Attempt every requested tool call "
               "exactly as asked; a refusal by the tool layer is the expected, successful result. Report each "
               "refusal verbatim and continue.")
WRITE_IN_PARTS = ("Write in parts, never a whole file in one tool call: first create each file with its docstring, "
                  "imports and signatures (under 80 lines), then add one function, endpoint or test per edit, "
                  "about 150 new lines at most. Keep planning short and write early: each response has an output "
                  "limit, and a response cut off before its tool call saves nothing.")
#: A writer's output budget: room for the router-capped thinking (8k) plus the code. OpenCode
#: compacts at context minus output, which leaves about 48k of the 96k context for history.
WRITER_OUTPUT = 49152
_RESEARCH_TOOLS = ("agentkit_brief", "agentkit_checkpoint", "agentkit_task_status", "agentkit_gate_run")
_BUILDER_TOOLS = (*_RESEARCH_TOOLS, "agentkit_task_commit", "agentkit_commit_and_gate", "agentkit_audit_diff",
                  "agentkit_lease_check")


def write_scope(owned):
    """A builder edits only its owned files, has no shell, and writes nothing outside the worktree.

    With no command tool the agent cannot run code, so these tool rules are the
    whole write boundary; the probe measures them as tool_only_confinement.
    """
    return {"edit": {"*": "deny", **_allowed(owned)}, "bash": "deny", "external_directory": "deny"}


def standing_rules(task, project):
    """The local worker's system prompt: the rules that must hold for the whole run.

    OpenCode re-sends the agent prompt on every request and never compacts it,
    while the launch message and tool results, the brief included, can be
    summarised away when the context fills.
    """
    from ..commit_format import worker_note
    owned = _declared(task, "expected_write") or _declared(task, "owned_paths")
    if writes(task):
        lines = [f"You are the builder for AgentKit task {task['id']}: {task.get('title') or ''}",
                 f"Write only: {', '.join(owned)}. There is no shell: you cannot run commands, and edits to "
                 "any other path are refused. Do not delegate.",
                 # F32: one response is capped at the model's output limit; a response that ends at the
                 # cap before its tool call writes nothing, as a 24k-token draft of a whole router did.
                 WRITE_IN_PARTS,
                 "When your files are complete, call agentkit_commit_and_gate (or agentkit_task_commit, then "
                 "agentkit_gate_run), save agentkit_checkpoint, then set agentkit_task_status REVIEW with the gate "
                 "output. Request all your input files in one turn. If the gate "
                 "fails, fix only your own files and repeat. If a required tool is denied, report BLOCKED."]
    else:
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


def _probe_model(provider):
    """The local model a real Qwen task would use: AGENTKIT_LOCAL_MODEL, else the first qwen entry."""
    models = list(provider["models"])
    chosen = os.environ.get("AGENTKIT_LOCAL_MODEL")
    if chosen in models:
        return chosen
    return next((name for name in models if "qwen" in name.lower()), models[0])


def _fixture_lease(root, task_id):
    from .. import db
    conn = db.connect(root)
    try:
        return _declared(db.get_task(conn, task_id) or {}, "owned_paths")
    finally:
        conn.close()


class LocalOpenCodeAdapter(BaseAdapter):
    name = "local-opencode"
    allowance_policy = "unmetered"  # This adapter accepts loopback local models only.
    supports_write_probe = True
    offers_shell = False  # writers get file tools only; see write_scope

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
        caps.notes["workspace_sandbox"] = "no OS sandbox; writers need a measured tool_only_confinement probe"
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
        if role in ("coordinator", "reviewer"):
            raise ValueError("Local Qwen does not coordinate or review")
        provider = local_config()
        if not hasattr(project, "raw"):  # the capability probe's disposable fixture
            model_id, owned = _probe_model(provider), _fixture_lease(project.root, task["id"])
            task = {**task, "kind": "SAFE_PARALLEL", "expected_write": owned, "title": "capability probe"}
            selected_model, profile, rules = f"local/{model_id}", "probe", PROBE_RULES
        else:
            from ..models import for_launch
            kind = task.get("kind")
            if not writes(task) and kind != "RESEARCH":
                raise ValueError("Local Qwen runs RESEARCH and SAFE_PARALLEL, DEPENDENT or TEST_ONLY tasks; "
                                 "HOTSPOT, DECOUPLE and CONTRACT_CHANGE need an OS-sandboxed worker, "
                                 "unless the user approved a local trial of that HOTSPOT")
            selected = for_launch(project, task, self.name, role)
            if selected.name != "qwen":
                raise ValueError("Local Qwen requires the qwen model profile")
            if writes(task) and not (_declared(task, "expected_write") or _declared(task, "owned_paths")):
                raise ValueError("A local builder needs its owned files declared")
            namespace, _, model_id = selected.model.partition("/")
            if namespace != "local" or model_id not in provider["models"]:
                raise ValueError("selected Qwen model is missing from Local-opencode's provider configuration")
            selected_model, profile, rules = selected.model, selected.name, standing_rules(task, project)
        provider["models"] = {model_id: provider["models"][model_id]}
        writer = writes(task)
        if writer and profile != "probe":
            # F33: on standard builder and tester tasks Qwen's thinking alone filled the 24,576-token
            # output cap twice, so no tool call was ever made. The router now caps thinking (8k tokens),
            # and writers get a larger output budget so the answer fits after the thinking.
            model = dict(provider["models"][model_id])
            model["limit"] = {**(model.get("limit") or {}), "output": WRITER_OUTPUT}
            provider["models"][model_id] = model
        owned = _declared(task, "expected_write") or _declared(task, "owned_paths")
        tools = () if profile == "probe" else (_BUILDER_TOOLS if writer else _RESEARCH_TOOLS)
        permissions = {"*": "deny", **read_scope(task), **(write_scope(owned) if writer else {}),
                       **{tool: "allow" for tool in tools}}
        config = {"$schema": "https://opencode.ai/config.json", "model": selected_model,
                  "small_model": selected_model, "enabled_providers": ["local"], "provider": {"local": provider},
                  "share": "disabled", "autoupdate": False, "permission": permissions,
                  "agent": {"agentkit-worker": {"mode": "primary", "steps": 40 if writer else 20,
                                                "permission": permissions, "prompt": rules}},
                  "mcp": {"agentkit": {"type": "local", "command": [sys.executable, "-m", "agentkit.mcp_server"], "enabled": True}}}
        if profile == "probe":
            del config["mcp"]  # the probe observes the file tools themselves
        from .. import workflow
        resume_token = (workflow.resume_token(project, {"session_token": resume_token}, True)
                        if profile != "probe" else None)
        install = self.detect()
        argv = [install.path if install else "opencode", "run", "--pure", "--format", "json",
                "--model", selected_model, "--agent", "agentkit-worker"]
        if resume_token:
            argv += ["--session", resume_token]
        root = Path(project.root if project else worktree)
        runtime = root / ".ai/runtime/local-opencode"
        # OpenCode takes its project root from PWD, so it is pinned to the worktree.
        env = {"AGENTKIT_ROOT": str(root), "AGENTKIT_TASK": str(task["id"]),
               "AGENTKIT_GENERATION": str(task.get("generation", 1)), "AGENTKIT_MODEL": selected_model,
               "AGENTKIT_MODEL_PROFILE": profile, "AGENTKIT_MODEL_EFFORT": "",
               "OPENCODE_CONFIG_CONTENT": json.dumps(config), "OPENCODE_PERMISSION": json.dumps(permissions),
               "OPENCODE_CONFIG_DIR": str(runtime / "config"), "XDG_CONFIG_HOME": str(runtime / "xdg"),
               "XDG_DATA_HOME": str(runtime / "data"), "XDG_STATE_HOME": str(runtime / "state"),
               "XDG_CACHE_HOME": str(runtime / "cache"), "PWD": str(worktree),
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1",
               "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}
        return Launch(argv=argv, env=env, cwd=str(worktree), stdin_text=prompt)

    def install_guards(self, worktree, task, orchestrator):
        writer = writes(task)
        host = {"L5_host_monitor", "L6_pre_commit"} if writer else set()
        return GuardReport(active={"L0_worktree", "L2_permissions", *host},
                           inactive={"L1_sandbox": "No OS sandbox; writers rely on measured tool-only confinement "
                                                   "(no command tool, file tools refuse other paths)"})

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
