"""`agentkit-hook <event>` — the entry point every agent's hooks call.

Contract with the harness (identical for Claude Code and Codex):

  * exit 0  — allow; stdout becomes context for the model
  * exit 2  — block; stderr is shown to the model as the reason
  * other   — hook error; does not block

Two rules keep this safe to install globally:

  1. **No `.ai/project.yaml`, no enforcement.** Repositories that never adopted
     AgentKit behave exactly as before.
  2. **Fail open, loudly.** A bug here must never wedge a session, so unexpected
     exceptions warn on stderr and exit 0. A blocked edit is cheap to retry; a
     session that can edit nothing is not. The layers that must not fail open —
     L6 and L7 — live in core and are not reached through this file's exception
     handler.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeGuard

from . import audit, briefs, db, repo, shellguard
from .checkpoints import mechanical_snapshot
from .config import load_project
from .console import use_utf8
from .context import active_task_id
from .hook_worktree_paths import (
    HookContextError,
    authority_root,
    supervised,
    validate_cwd,
    write_context,
)
from .leases import Decision, decide
from .paths import (
    canonical_relpath,
    find_project_root,
    git_root,
    is_absolute_like,
    repo_common_root,
)

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Update", "apply_patch"}
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "exec_command", "local_shell"}


def _warn(message: str) -> None:
    """The 'loudly' half of fail-open.

    A hook that cannot read its payload allows every write that batch. That is the
    right default — wedging a session is worse — but it must not be silent, or an
    enforcement layer can be off for an entire run with nothing to show for it.
    """
    with suppress(OSError, ValueError):
        sys.stderr.write(f"[AgentKit] {message}\n")


def _read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        _warn(f"could not read hook payload ({exc}); enforcement skipped for this call.")
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(
            f"hook payload is not valid JSON ({exc}); enforcement skipped for this "
            "call. Lease checks did NOT run — the merge gate is still the backstop."
        )
        return {}
    if not isinstance(data, dict):
        _warn(
            f"hook payload was a {type(data).__name__}, expected an object; "
            "enforcement skipped for this call."
        )
        return {}
    return data


def _target_paths(tool_input: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("file_path", "path", "notebook_path", "filePath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("file_path") or edit.get("path")
                if isinstance(value, str) and value.strip():
                    found.append(value)
    return found


def _command_text(tool_input: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script", "input"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            return " ".join(str(v) for v in value)
    return ""


def _context(payload: dict[str, Any], *, writing: bool = False) -> tuple[Path | None, int | None]:
    if writing:
        validate_cwd(payload)
    cwd = payload.get("cwd") or payload.get("workspace") or payload.get("worktree") or None
    root = find_project_root(cwd)
    return root, active_task_id(cwd)


def _managed(root: Path | None) -> TypeGuard[Path]:
    """True when this repository is onboarded — and narrows `root` to `Path`.

    A TypeGuard rather than a plain bool so the checker enforces what the code
    already relies on: every caller returns early when this is False, and nothing
    downstream should have to re-handle None.
    """
    return bool(root and (root / ".ai" / "project.yaml").is_file())


def _block(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 2


# ------------------------------------------------------------------ L3: files


def handle_pre_tool_use(payload: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_name not in WRITE_TOOLS | SHELL_TOOLS:
        return 0

    root, task_id = _context(payload, writing=True)
    root = authority_root(root, task_id)
    if task_id is None and tool_name in WRITE_TOOLS and not supervised() and _managed(root):
        return _guard_untasked_writes(payload, tool_name, _target_paths(tool_input))
    if not _managed(root):
        return 0

    if tool_name in SHELL_TOOLS:
        return _guard_shell(root, task_id, _command_text(tool_input), payload)
    if tool_name not in WRITE_TOOLS:
        return 0

    targets = _target_paths(tool_input)
    if not targets:
        return 0

    project = load_project(root)
    conn = db.connect(root)
    try:
        scope = write_context(conn, root, task_id, payload)
        for raw in targets:
            rel = scope.relative(raw)
            if rel is None:
                return _block(
                    f"[AgentKit] Edit blocked.\n`{raw}` is outside the repository. "
                    "Workers may only modify files inside their own worktree."
                )
            verdict = decide(conn, project, rel, task_id)
            if not verdict.allowed:
                db.record_violation(conn, task_id, "L3", rel, verdict.reason,
                                    channel=f"tool:{tool_name}")
                return _block(f"[AgentKit] Edit blocked.\n{verdict.reason}")
        if task_id is not None:
            db.heartbeat(conn, task_id, scope.generation)
    finally:
        conn.close()
    return 0


def _guard_untasked_writes(payload: dict[str, Any], tool_name: str, targets: list[str]) -> int:
    """A session with no task: judge each file by the repository that contains it.

    Leases belong to a repository. A root or operator session started in one
    checkout must not be refused a file in another, or one outside every managed
    repository, where there is no lease to protect. A file inside a managed
    repository, including one of its linked worktrees, still needs that
    repository's operator lease through the same `decide()` every layer uses.
    """
    cwd = Path(str(payload.get("cwd") or payload.get("workspace") or os.getcwd()))
    for raw in targets:
        target = Path(raw) if is_absolute_like(raw) else cwd / raw
        probe = target.parent
        while not probe.is_dir() and probe != probe.parent:
            probe = probe.parent
        root = find_project_root(probe)
        if not _managed(root):
            continue
        checkout = root
        top = git_root(probe)
        common = repo_common_root(probe)
        if top is not None and common is not None and common.resolve() == root.resolve():
            checkout = top  # a linked worktree of this repository; leases use repo paths
        rel = canonical_relpath(target, checkout)
        project = load_project(root)
        conn = db.connect(root)
        try:
            if rel is None:
                verdict = Decision(False, f"`{raw}` resolves outside its checkout `{checkout}`.",
                                   "outside_worktree")
            else:
                verdict = decide(conn, project, rel, None)
            if not verdict.allowed:
                db.record_violation(conn, None, "L3", rel or raw, verdict.reason,
                                    channel=f"tool:{tool_name}")
                return _block(f"[AgentKit] Edit blocked.\n{verdict.reason}")
        finally:
            conn.close()
    return 0


# ------------------------------------------------------------------ L4: shell


def handle_pre_bash(payload: dict[str, Any]) -> int:
    tool_input = payload.get("tool_input")
    command = _command_text(tool_input) if isinstance(tool_input, dict) else ""
    root, task_id = _context(payload, writing=True)
    root = authority_root(root, task_id)
    if not _managed(root):
        return 0
    return _guard_shell(root, task_id, command, payload)


def _guard_shell(root: Path, task_id: int | None, command: str, payload: dict[str, Any]) -> int:
    if not command.strip():
        return 0
    if task_id is None:
        return 0                      # unmanaged session: no lease to enforce

    project = load_project(root)
    conn = db.connect(root)
    try:
        scope = write_context(conn, root, task_id, payload)
        def authorize(path: str):
            rel = scope.relative(path)
            if rel is None:
                return Decision(False, "Target is outside the assigned worktree", "outside_worktree")
            return decide(conn, project, rel, task_id)

        allowlist = _allowlisted_commands(project, db.get_task(conn, task_id)) if scope.cwd == scope.worktree else []
        verdict = shellguard.classify(command, authorize, allowlist)
        if not verdict.allowed:
            db.record_violation(conn, task_id, "L4", ", ".join(verdict.writes) or command[:200],
                                verdict.reason, channel="shell")
            return _block(
                "[AgentKit] Command blocked.\n"
                f"{verdict.reason}\n"
                "If this is a project command, add it to `allowlisted_commands` in "
                ".ai/project.yaml. If you need a file you do not own, call `lease_request`."
            )
        db.heartbeat(conn, task_id, scope.generation)
    finally:
        conn.close()
    return 0


def _allowlisted_commands(project: Any, task=None) -> list[str]:
    """The project's own declared gate commands are trusted by construction."""
    from .workflow import enabled
    if enabled(project) and task:
        return project.gate(task["gate_level"])
    allow = list(project.raw.get("allowlisted_commands") or []) if project.raw else []
    for commands in project.gates.values():
        allow.extend(commands)
    return [str(a) for a in allow]


# ------------------------------------------------------------ L5: post-hoc audit


def handle_post_tool_use(payload: dict[str, Any]) -> int:
    """Detection for every channel L3 and L4 cannot see.

    Runs async, so it never slows a tool call. It does not block the tool that
    already ran — it records the violation and tells the model, which is what
    "detection" honestly means.
    """
    root, task_id = _context(payload)
    if not _managed(root) or task_id is None:
        return 0
    project = load_project(root)
    from .workflow import enabled
    if enabled(project) and os.environ.get("AGENTKIT_AUDIT_OWNER") == "monitor":
        # Virtual readonly masks can look untracked inside the worker shell.
        # The independently running host monitor audits the real filesystem.
        return 0
    conn = db.connect(root)
    try:
        worktree = payload.get("cwd") or root
        result = audit.audit_worktree(conn, project, worktree, task_id)
        if not result.clean:
            return _block(
                "[AgentKit] Out-of-lease changes detected in your worktree.\n"
                f"{result.summary()}\n"
                "Revert these files. They will be rejected at the merge gate, and they may "
                "belong to another agent working right now."
            )
    finally:
        conn.close()
    return 0


# ------------------------------------------------------------- L6: pre-commit


def handle_pre_commit(payload: dict[str, Any]) -> int:
    """Called by git, not by an agent. Must not fail open silently.

    Exit 1 is git's "reject the commit"; the wrapper below deliberately does not
    swallow exceptions for this event.

    `root` and `cwd` differ here and the distinction is load-bearing: config and
    the task database come from the main checkout, while the staged files being
    judged are the ones in *this* worktree.
    """
    cwd = os.getcwd()
    root = find_project_root(cwd)
    task_id = active_task_id(cwd)
    if not _managed(root):
        sys.stderr.write(
            "[AgentKit] pre-commit hook ran but found no .ai/project.yaml; "
            "allowing the commit. If this repository is managed, the hook is "
            "pointing at the wrong root.\n"
        )
        return 0
    project = load_project(root)
    # A commit runs inside the worker shell sandbox. Read authority without
    # migrating the database or requiring write permission to shared state.
    conn = db.connect_readonly(root)
    try:
        result = audit.audit_staged(conn, project, cwd, task_id)
        if result.clean:
            return 0
        # Host-side L5/L7 record violations. L6 must reject even when its
        # sandbox cannot write the shared event database.
        sys.stderr.write(
            "[AgentKit] Commit rejected: it contains changes outside this task's lease.\n"
            f"{result.summary()}\n"
            "Unstage those files. Committing them would be rejected at the merge gate anyway.\n"
        )
        return 1
    finally:
        conn.close()


# ------------------------------------------------------- session & checkpoints


def handle_session_start(payload: dict[str, Any]) -> int:
    root, task_id = _context(payload)
    if not _managed(root) or task_id is None:
        return 0
    project = load_project(root)
    conn = db.connect(root)
    try:
        brief = briefs.build(conn, project, task_id)
        if brief is None:
            return 0
        token = payload.get("session_id")
        if isinstance(token, str) and token:
            db.update_task(conn, task_id, session_token=token)
        db.log_event(conn, task_id, "session_start", cause="worker session opened",
                     detail={"session": token})
        sys.stdout.write(briefs.render(brief) + "\n")
    finally:
        conn.close()
    return 0


def _auto_checkpoint(payload: dict[str, Any], reason: str) -> int:
    root, task_id = _context(payload)
    if not _managed(root) or task_id is None:
        return 0
    conn = db.connect(root)
    try:
        worktree = payload.get("cwd") or root
        snapshot = mechanical_snapshot(conn, root, worktree, task_id, reason=reason)
        db.write_checkpoint(
            conn, task_id, snapshot, kind="mechanical", reason=reason,
            head_sha=str(snapshot.get("head_sha") or ""),
            generation=int(snapshot.get("generation") or 0),
        )
        db.heartbeat(conn, task_id)
        if snapshot.get("head_sha"):
            db.update_task(conn, task_id, last_commit=snapshot["head_sha"])
    finally:
        conn.close()
    return 0


def handle_pre_compact(payload: dict[str, Any]) -> int:
    return _auto_checkpoint(payload, "pre_compact")


def handle_stop(payload: dict[str, Any]) -> int:
    return _auto_checkpoint(payload, "stop")


def handle_worktree_create(payload: dict[str, Any]) -> int:
    root, task_id = _context(payload)
    if root is None or task_id is None:
        return 0
    conn = db.connect(root)
    try:
        worktree = payload.get("worktree_path") or payload.get("path") or payload.get("cwd")
        if worktree:
            db.update_task(conn, task_id, worktree=str(worktree),
                           base_sha=repo.head_commit(str(worktree)))
        db.log_event(conn, task_id, "worktree_created", detail={"path": str(worktree or "")})
    finally:
        conn.close()
    return 0


def handle_permission_denied(payload: dict[str, Any]) -> int:
    """A denied permission is the clearest signal that a task is mis-scoped."""
    root, task_id = _context(payload)
    if root is None:
        return 0
    conn = db.connect(root)
    try:
        db.log_event(
            conn, task_id, "permission_denied",
            cause=str(payload.get("tool_name") or "unknown tool"),
            detail={"detail": str(payload.get("tool_input"))[:500]},
        )
    finally:
        conn.close()
    return 0


HANDLERS = {
    "pre-tool-use": handle_pre_tool_use,
    "pre-bash": handle_pre_bash,
    "post-tool-use": handle_post_tool_use,
    "pre-commit": handle_pre_commit,
    "session-start": handle_session_start,
    "pre-compact": handle_pre_compact,
    "stop": handle_stop,
    "worktree-create": handle_worktree_create,
    "permission-denied": handle_permission_denied,
}

#: Events where failing open would defeat the layer. These do not swallow errors.
STRICT_EVENTS = {"pre-commit"}


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        sys.stderr.write("usage: agentkit-hook <event>\n")
        return 0
    event = args[0]
    if event == "codex-pre-tool-use":
        from .codex_hooks import main as codex_main
        return codex_main()
    handler = HANDLERS.get(event)
    if handler is None:
        return 0
    payload = _read_input()
    if event in STRICT_EVENTS:
        return handler(payload)
    try:
        return handler(payload)
    except HookContextError as exc:
        return _block(f"[AgentKit] Worker write blocked.\n{exc}")
    except Exception as exc:  # fail open, loudly — see module docstring
        sys.stderr.write(f"[AgentKit] hook '{event}' error (allowing): {exc}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
