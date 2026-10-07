"""L4 — deciding whether a shell command may write outside a task's lease.

Shell is not statically decidable. `python -c "$(curl ...)"` can write anywhere
and no parser will tell you where. So this module is explicit about the boundary
of its own knowledge:

* commands it can prove write only in-scope     -> ALLOW
* commands it can prove write out of scope      -> DENY
* commands whose write targets it cannot prove  -> DENY, unless the project has
  explicitly allowlisted them (its own declared gate commands are seeded in)

Deny-by-default on the unprovable half is the only honest policy. The cost is
occasionally blocking something harmless, and the agent is told exactly how to
proceed; the alternative cost is a silent out-of-lease write that only L5 catches
after the fact.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

#: Programs that cannot write. Safe with no redirection.
READONLY = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "rg", "egrep", "fgrep",
    "find", "fd", "ls", "dir", "tree", "wc", "sort", "uniq", "cut", "awk",
    "diff", "stat", "file", "which", "where", "echo", "printf", "pwd", "date",
    "env", "whoami", "hostname", "du", "df", "ps", "top", "sleep", "true", "false",
    "basename", "dirname", "realpath", "readlink", "jq", "yq", "column", "tee",
})

#: Git subcommands that only read.
GIT_READONLY = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "rev-parse", "describe",
    "blame", "shortlog", "ls-files", "ls-tree", "cat-file", "merge-base",
    "config", "worktree", "stash",
})

#: Git subcommands that mutate the working tree or history.
GIT_MUTATORS = frozenset({
    "checkout", "restore", "reset", "clean", "apply", "revert", "cherry-pick",
    "rebase", "merge", "rm", "mv", "switch",
})

#: Programs that write, with the target derivable from the arguments.
TARGETED_MUTATORS = frozenset({"cp", "mv", "rm", "install", "touch", "mkdir", "ln", "rsync"})

#: In-place editors: target derivable, but the flag matters.
INPLACE_EDITORS = {"sed": "-i", "perl": "-i", "ruby": "-i"}

#: Programs whose write targets are not knowable from the command line.
OPAQUE = frozenset({
    "python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php",
    "bash", "sh", "zsh", "powershell", "pwsh", "cmd", "make", "cmake", "ninja",
    "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "pdm", "cargo",
    "go", "dotnet", "gradle", "mvn", "docker", "docker-compose",
})

#: Formatters and generators that rewrite whole trees when given a broad path.
BROAD_WRITERS = {
    "ruff": ("format", "--fix"),
    "black": (),
    "prettier": ("--write", "-w"),
    "eslint": ("--fix",),
    "isort": (),
    "gofmt": ("-w",),
    "rustfmt": (),
    "clang-format": ("-i",),
}

UNKNOWN = "<unknown>"

_SPLIT = re.compile(r"\s*(?:\|\||&&|;|\||\n)\s*")
_REDIRECT = re.compile(r"(?:^|\s)(?:\d?>>?|&>)\s*(\S+)")


@dataclass
class ShellVerdict:
    allowed: bool
    reason: str
    code: str
    writes: list[str] = field(default_factory=list)
    opaque: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "reason": self.reason, "code": self.code,
            "writes": self.writes, "opaque": self.opaque,
        }


def _tokenize(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _basename(program: str) -> str:
    name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def write_targets(command: str) -> tuple[list[str], bool]:
    """Return (paths this command writes, whether any target is unprovable)."""
    writes: list[str] = []
    opaque = False

    for segment in _SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue

        for match in _REDIRECT.finditer(segment):
            target = match.group(1)
            if target not in ("/dev/null", "NUL", "nul"):
                writes.append(target)

        tokens = _tokenize(_REDIRECT.sub(" ", segment))
        if not tokens:
            continue
        # Strip leading VAR=value assignments.
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
        if not tokens:
            continue

        program = _basename(tokens[0])
        args = tokens[1:]

        if program == "git":
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub in GIT_MUTATORS:
                paths = _after_double_dash(args)
                if paths:
                    writes.extend(paths)
                else:
                    opaque = True
            continue

        if program in READONLY:
            continue

        if program in INPLACE_EDITORS:
            flag = INPLACE_EDITORS[program]
            if any(a == flag or a.startswith(flag) for a in args):
                writes.extend(_path_like(args))
            continue

        if program in TARGETED_MUTATORS:
            paths = _path_like(args)
            if program in ("cp", "mv", "ln", "rsync") and len(paths) >= 2:
                writes.append(paths[-1])          # only the destination is written
            elif paths:
                writes.extend(paths)
            else:
                opaque = True
            continue

        if program in BROAD_WRITERS:
            flags = BROAD_WRITERS[program]
            writing = not flags or any(a in flags for a in args)
            if writing:
                paths = _path_like(args)
                writes.extend(paths or [UNKNOWN])
            continue

        if program in OPAQUE:
            opaque = True
            continue

        # An unrecognised program is assumed capable of writing.
        opaque = True

    if UNKNOWN in writes:
        opaque = True
        writes = [w for w in writes if w != UNKNOWN]
    return writes, opaque


def _after_double_dash(args: list[str]) -> list[str]:
    if "--" in args:
        return [a for a in args[args.index("--") + 1:] if not a.startswith("-")]
    return []


def _path_like(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-") and a not in ("--",)]


def classify(
    command: str,
    authorize,                      # (path) -> object with .allowed and .reason
    allowlist: list[str] | None = None,
) -> ShellVerdict:
    """Decide whether a shell command may run under the current lease."""
    if not command.strip():
        return ShellVerdict(True, "empty command", "empty")

    if allowlist and _matches_allowlist(command, allowlist):
        return ShellVerdict(
            True, "matches a command this project declares it runs", "allowlisted",
        )

    writes, opaque = write_targets(command)

    blocked = []
    for target in writes:
        verdict = authorize(target)
        if not verdict.allowed:
            blocked.append((target, verdict.reason))

    if blocked:
        target, reason = blocked[0]
        return ShellVerdict(
            False,
            f"this command writes `{target}`, which is not yours. {reason}",
            "shell_out_of_scope",
            writes=[t for t, _ in blocked],
        )

    if opaque:
        return ShellVerdict(
            False,
            "this command can write files but its targets cannot be determined from the "
            "command line, so it cannot be proven to stay inside your lease. Use your file "
            "editing tool instead, or ask for the command to be added to "
            "`allowlisted_commands` in .ai/project.yaml if it is a project command.",
            "opaque_write",
            opaque=True,
        )

    return ShellVerdict(True, "no out-of-scope writes detected", "ok", writes=writes)


def _matches_allowlist(command: str, allowlist: list[str]) -> bool:
    normalized = " ".join(command.split())
    for entry in allowlist:
        pattern = " ".join(str(entry).split())
        if not pattern:
            continue
        if normalized == pattern or normalized.startswith(pattern + " "):
            return True
        if pattern.endswith("*") and normalized.startswith(pattern[:-1]):
            return True
    return False
