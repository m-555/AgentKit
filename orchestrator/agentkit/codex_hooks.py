"""Scoped Codex PreToolUse lease checks; malformed supervised calls deny.

Codex 0.159.2 reports apply_patch and Bash with tool_input.command. Explicit
JSON deny and exit 2 are supported. Trust or callback failure can still skip
this guard, so installation alone never certifies runtime enforcement.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from . import audit, db, shellguard, worker
from .config import load_project
from .console import use_utf8
from .leases import Decision, decide
from .patch_paths import patch_targets
from .paths import canonical_relpath, is_absolute_like
from .secrets import redact

SHELL_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "PowerShell"}
CONTROL_PATHS = {".codex/hooks.json", ".codex/config.toml", ".codex/project.rules"}
READ_COMMANDS = {"cat", "head", "tail", "ls", "dir", "pwd", "wc", "grep", "rg", "stat",
                 "echo", "printf", "get-content", "get-childitem", "get-item", "get-location", "write-output"}
READ_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "cat-file", "blame"}


def supervised() -> bool:
    return any(os.environ.get(key) for key in ("AGENTKIT_TASK", "AGENTKIT_PROCESS", "AGENTKIT_WORKTREE"))


def deny(reason: str) -> int:
    message = "[AgentKit] Codex tool blocked. " + str(redact(reason))[:1800]
    # JSON is valid even if Codex chooses the stdout contract; stderr/2 also deny.
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": message}}) + "\n")
    sys.stderr.write(message + "\n")
    return 2


def _directory(raw, base: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("Missing or malformed execution directory")
    path = Path(raw)
    if not path.is_absolute():
        if base is None or is_absolute_like(raw):
            raise ValueError("Execution directory is not an absolute native path")
        path = base / path
    return path.resolve(strict=True)


def _within_directory(directory: Path, worktree: Path) -> bool:
    return directory == worktree or canonical_relpath(directory, worktree, allow_missing=False) is not None


def _target(raw: str, cwd: Path, worktree: Path) -> str | None:
    # Refuse URI and Windows alternate data streams; neither is a normal lease path.
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw or "://" in raw:
        return None
    normalized = raw.replace("\\", "/")
    if ":" in normalized[2:] or (":" in normalized and not (len(normalized) > 2 and normalized[1] == ":")):
        return None
    path = Path(raw)
    if is_absolute_like(raw) and not path.is_absolute():
        return None
    if not is_absolute_like(raw):
        path = cwd / raw
    rel = canonical_relpath(path, worktree)
    if rel is None or rel.casefold() in CONTROL_PATHS or rel.casefold() == ".git" or rel.casefold().startswith(".git/"):
        return None
    return rel


def _shell(command: str, authorize, allowlist: list[str], commit_check=None,
           *, worktree: Path | None = None, reliable_cwd: bool = False) -> shellguard.ShellVerdict:
    # An explicitly declared gate is trusted only verbatim, never by prefix. It
    # is available only at the worktree root (the caller enforces that condition).
    if reliable_cwd and " ".join(command.split()) in {" ".join(value.split()) for value in allowlist}:
        return shellguard.ShellVerdict(True, "exact project command", "allowlisted")
    if "<" in command:
        return shellguard.ShellVerdict(False, "Shell input redirection is not supported", "opaque_write")
    if any(char in command for char in ";|&$`\n\r()") or (os.name == "nt" and any(char in command for char in "%!")):
        return shellguard.ShellVerdict(False, "Compound or expanding shell command cannot be proven safe", "opaque_write")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=">")
    lexer.whitespace_split, lexer.commenters = True, ""
    if os.name == "nt":
        lexer.escape = ""  # Backslashes are path separators in PowerShell/cmd.
    tokens = list(lexer)
    writes = []
    arguments = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in (">", ">>"):
            index += 1
            if index == len(tokens) or tokens[index] in (">", ">>"):
                raise ValueError("Missing shell redirection target")
            writes.append(tokens[index])
        elif ">" in token:
            raise ValueError("Unknown shell redirection syntax")
        else:
            arguments.append(token)
        index += 1
    if not arguments:
        raise ValueError("Missing shell command")
    program = arguments[0].lower()
    args = arguments[1:]
    # No executable paths, aliases, or dynamic program lookup are inferred safe.
    commit = False
    if program == "git":
        pinned = None
        no_pager = False
        while args and args[0] in ("-C", "--no-pager"):
            option = args.pop(0)
            if option == "--no-pager":
                no_pager = True
            elif pinned is None and args:
                raw = args.pop(0)
                if not Path(raw).is_absolute():
                    raise ValueError("Git -C must be an absolute assigned worktree")
                pinned = _directory(raw)
            else:
                raise ValueError("Missing or repeated Git -C directory")
        if pinned is not None and pinned != worktree:
            raise ValueError("Git -C is not the exact authoritative worktree")
        if args and args[0] == "add":
            paths = args[2:] if len(args) > 1 and args[1] == "--" else args[1:]
            safe = pinned == worktree and pinned is not None and bool(paths) and all(
                Path(value).is_absolute() and not value.startswith("-") for value in paths)
            writes.extend(paths)
        elif args and args[0] == "commit":
            # No editor, implicit restaging, alternate index, or skipped hooks.
            index = 1
            safe = bool(commit_check) and pinned == worktree and pinned is not None and len(args) > 2
            while index < len(args) and safe:
                if args[index] in ("-m", "--message") and index + 1 < len(args) and args[index + 1]:
                    index += 2
                elif args[index].startswith("--message=") and args[index][len("--message="):]:
                    index += 1
                else:
                    safe = False
            commit = safe
        else:
            # write_stdin has no PreToolUse callback. Suppress Git pagers so an
            # allowed read cannot open an interactive command execution channel.
            safe = no_pager and bool(args) and args[0] in READ_GIT and not any(
                value.startswith(("--ext-diff", "--textconv", "--filters", "--output", "-o")) for value in args[1:])
    elif program == "set-content":
        # Exactly one literal path and value; no wildcard, encoding/provider switches or pipelines.
        safe = (len(args) in (4, 5) and args[0].lower() == "-literalpath"
                and args[2].lower() == "-value" and
                (len(args) == 4 or args[4].lower() == "-nonewline"))
        if safe:
            writes.append(args[1])
    elif program in READ_COMMANDS:
        safe = not any(value.startswith(("--pre", "--hostname-bin")) for value in args)
    elif program in {"touch", "mkdir", "rm", "mv", "cp", "tee"}:
        safe = bool(args) and not any(value.startswith("-") for value in args)
        writes.extend(args if program != "cp" else args[-1:])
    else:
        safe = False
    if not safe:
        return shellguard.ShellVerdict(False, "Command has unprovable writes; use apply_patch or an exact declared gate", "opaque_write")
    for path in writes:
        # The installed exec_command hook omits its actual per-call workdir.
        # Its common cwd is only the session cwd and cannot authorize relatives.
        if not reliable_cwd and not Path(path).is_absolute():
            return shellguard.ShellVerdict(False, "Actual shell workdir is unavailable; write targets must be absolute", "opaque_write")
        if any(char in path for char in "*?[]{}"):
            return shellguard.ShellVerdict(False, "Expanding shell write targets cannot be proven safe", "opaque_write")
        decision = authorize(path)
        if not decision.allowed:
            return shellguard.ShellVerdict(False, decision.reason, "shell_out_of_scope", writes=writes)
    if commit:
        return commit_check()
    return shellguard.ShellVerdict(True, "simple command writes only leased paths", "ok", writes=writes)


def handle(payload: dict) -> int:
    if not supervised():
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        raise ValueError("Missing or unsupported Codex hook event")
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("Missing or malformed supervised tool name")
    if tool == "write_stdin":
        return deny("Interactive shell input is not covered by Codex PreToolUse checks")
    if tool != "apply_patch" and tool not in SHELL_TOOLS:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str) or not tool_input["command"].strip():
        raise ValueError("Missing or malformed supervised command payload")
    root = _directory(os.environ.get("AGENTKIT_ROOT"))
    if not (root / ".ai/project.yaml").is_file() or not (root / ".ai/tasks.db").is_file():
        raise ValueError("Authoritative managed state is unavailable")
    if os.environ.get("AGENTKIT_CODEX_READONLY") == "1":
        worktree = _directory(os.environ.get("AGENTKIT_WORKTREE"))
        cwd = _directory(payload.get("cwd"))
        if not _within_directory(cwd, worktree):
            raise ValueError("Read-only control cwd is outside its assigned worktree")
        if tool == "apply_patch":
            return deny("This control session is read-only")
        verdict = _shell(tool_input["command"], lambda _: Decision(False, "Control session is read-only", "readonly"), [])
        return 0 if verdict.allowed else deny(verdict.reason)
    raw_task = os.environ.get("AGENTKIT_TASK", "")
    raw_generation = os.environ.get("AGENTKIT_GENERATION", "")
    if not raw_task.isdigit() or not raw_generation.isdigit():
        raise ValueError("Supervised task and generation must be explicit integers")
    task_id, generation = int(raw_task), int(raw_generation)
    conn = db.connect(root)
    try:
        ctx = worker.require_current(conn, task_id, worker_generation=generation)
        # Never authorize against the primary authority root when editing a
        # linked worktree. The launcher provides its exact assigned checkout.
        worktree = _directory(os.environ.get("AGENTKIT_WORKTREE") or ctx.task.get("worktree"))
        if ctx.task.get("worktree") and _directory(ctx.task["worktree"]) != worktree:
            raise ValueError("Assigned worktree disagrees with authoritative task state")
        cwd = _directory(payload.get("cwd"))
        if not _within_directory(cwd, worktree):
            raise ValueError("Hook cwd is outside the assigned worktree")
        reliable_cwd = False
        if tool in SHELL_TOOLS:
            directories = [tool_input[key] for key in ("work_dir", "workdir", "cwd")
                           if key in tool_input and tool_input[key] is not None]
            if "work_dir" in payload and payload["work_dir"] is not None:
                directories.append(payload["work_dir"])
            actual = [_directory(value, cwd) for value in directories]
            if actual and any(value != actual[0] for value in actual):
                raise ValueError("Conflicting shell work directories")
            reliable_cwd = bool(actual)
            cwd = actual[0] if actual else cwd
            if not _within_directory(cwd, worktree):
                raise ValueError("Shell work directory is outside the assigned worktree")
        project = load_project(root)

        def authorize(raw):
            relative = _target(raw, cwd, worktree)
            if relative is None:
                return Decision(False, "Target is outside the assigned worktree or controls enforcement", "outside")
            if tool in SHELL_TOOLS and (worktree / relative).is_dir():
                return Decision(False, "Shell directory mutations need explicitly determined file targets", "opaque_write")
            return decide(conn, project, relative, task_id)

        command = tool_input["command"]
        if tool == "apply_patch":
            paths = patch_targets(command)
            for raw in paths:
                decision = authorize(raw)
                if not decision.allowed:
                    db.record_violation(conn, task_id, "L3", raw, decision.reason, channel="tool:apply_patch")
                    return deny(decision.reason)
        else:
            allowlist = list(project.raw.get("allowlisted_commands") or [])
            for commands in project.gates.values():
                allowlist.extend(commands)
            def commit_check():
                staged = audit.audit_staged(conn, project, worktree, task_id)
                if not staged.clean:
                    return shellguard.ShellVerdict(False, staged.summary(), "staged_out_of_scope", writes=staged.checked)
                for path in staged.checked:
                    decision = authorize(str(worktree / path))
                    if not decision.allowed:
                        return shellguard.ShellVerdict(False, decision.reason, "staged_out_of_scope", writes=staged.checked)
                return shellguard.ShellVerdict(True, "Staged paths passed the lease audit; Git pre-commit hooks still run", "staged_clean")

            verdict = _shell(command, authorize, [str(value) for value in allowlist] if cwd == worktree else [],
                             commit_check, worktree=worktree, reliable_cwd=reliable_cwd)
            if not verdict.allowed:
                db.record_violation(conn, task_id, "L4", ", ".join(verdict.writes) or command[:200], verdict.reason, channel="shell")
                return deny(verdict.reason)
        db.heartbeat(conn, task_id, generation)
    finally:
        conn.close()
    return 0


def main() -> int:
    use_utf8()
    if not supervised():
        return 0
    try:
        raw = sys.stdin.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Oversized Codex hook input")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Codex hook input must be an object")
        return handle(payload)
    except Exception as error:
        return deny(f"Lease checks could not complete: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
