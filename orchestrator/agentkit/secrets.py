"""Keeping credentials out of workers, and out of everything AgentKit persists (§5).

Tool-level deny rules like `Read(.env)` are worth having, but they only cover one
channel — `cat .env`, `python -c "print(open('.env').read())"` and a symlink
pointing at `~/.aws/credentials` all walk straight past them. So the rule that
actually matters is stronger:

    **A real secret should not exist inside a worker-accessible worktree.**

This module implements the parts of that AgentKit controls:

* a worker's environment is built from an allowlist, not inherited wholesale;
* a worktree is scanned for secret files and symlinks that escape to them;
* every string AgentKit persists — events, checkpoints, gate output, violation
  records — passes through `redact()` at the logging boundary, so no individual
  caller has to remember.

Redaction is defence in depth, not permission to be careless: a redacted secret
in a log is still a secret that reached a process that should not have had it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Filenames that hold credentials. Matched case-insensitively.
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "credentials.json", "service-account*.json", ".netrc", ".npmrc", ".pypirc",
    "secrets.y*ml", "*.secrets", ".htpasswd",
)

#: Directories that must never be reachable from a worktree.
SECRET_DIRS = (".aws", ".ssh", ".gnupg", ".kube", ".docker", ".azure", ".config/gcloud")

#: Environment variables a worker gets. Everything else is dropped.
ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USERPROFILE", "USER", "USERNAME", "LOGNAME",
    "TMPDIR", "TEMP", "TMP", "SHELL", "COMSPEC", "PATHEXT",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "COLORTERM", "NUMBER_OF_PROCESSORS",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE",
    "UV_CACHE_DIR", "PIP_CACHE_DIR", "npm_config_cache", "NODE_OPTIONS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "AGENTKIT_TASK", "AGENTKIT_GENERATION", "AGENTKIT_ROOT",
    "AGENTKIT_PROCESS", "AGENTKIT_ROLE", "AGENTKIT_JOB", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "AGENTKIT_LOCAL_OPENCODE",
})

#: Variables whose *name* implies a credential, dropped even if allowlisted.
SECRET_NAME_HINTS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY", "PRIVATE_KEY",
    "CREDENTIAL", "SESSION_KEY", "ACCESS_KEY", "CLIENT_SECRET", "AUTH",
)

#: Credentials that merely *look* like one in free text.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("private-key-block", re.compile(
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----")),
    ("url-credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*)://[^\s/:@]+:[^\s/@]+@")),
    ("assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:"
        + "|".join(SECRET_NAME_HINTS)
        + r")[A-Z0-9_]*)\s*[=:]\s*[\"']?([^\s\"',;]{8,})")),
)

REDACTED = "[redacted]"


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Strip anything credential-shaped. Applied at the persistence boundary.

    Recurses through dicts and lists so a nested checkpoint payload is covered by
    one call rather than by every caller remembering.
    """
    if _depth > 12:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_secret_name(str(key)) else redact(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        cleaned = [redact(item, _depth=_depth + 1) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def redact_text(text: str) -> str:
    if not text:
        return text
    cleaned = text
    for name, pattern in _PATTERNS:
        if name == "assignment":
            cleaned = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", cleaned)
        elif name == "url-credentials":
            cleaned = pattern.sub(lambda m: f"{m.group(1)}://{REDACTED}@", cleaned)
        else:
            cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


# ------------------------------------------------------------- worker environment


def worker_environment(
    extra: dict[str, str] | None = None, *, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Build a worker's environment from an allowlist rather than inheriting.

    Inheriting `os.environ` hands every worker whatever credentials happen to be
    exported in the shell that launched the orchestrator — cloud keys, database
    URLs, registry tokens. A worker needs almost none of that.
    """
    source = base if base is not None else dict(os.environ)
    env: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper not in ENV_ALLOWLIST:
            continue
        if _is_secret_name(upper):
            continue
        env[key] = value
    for key, value in (extra or {}).items():
        env[key] = value
    return env


def dropped_variables(base: dict[str, str] | None = None) -> list[str]:
    """Which variables were withheld — reported by `agentkit doctor`, never logged."""
    source = base if base is not None else dict(os.environ)
    kept = set(worker_environment(base=source))
    return sorted(k for k in source if k not in kept)


# ----------------------------------------------------------------- worktree scan


def _matches(name: str, patterns: Iterable[str]) -> bool:
    from fnmatch import fnmatch

    lowered = name.lower()
    return any(fnmatch(lowered, pattern.lower()) for pattern in patterns)


def scan_worktree(worktree: str | Path, *, max_entries: int = 20_000) -> dict[str, list[str]]:
    """Find secrets reachable from a worktree, and links that escape to them.

    Returns `{"files": [...], "escaping_links": [...]}`, both repo-relative.
    """
    root = Path(worktree).resolve()
    files: list[str] = []
    links: list[str] = []
    seen = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv"}]
        current = Path(dirpath)

        for name in list(dirnames):
            candidate = current / name
            if candidate.is_symlink() and _escapes(candidate, root):
                links.append(_rel(candidate, root))
                dirnames.remove(name)
            elif _matches(name, SECRET_DIRS):
                files.append(_rel(candidate, root))

        for name in filenames:
            seen += 1
            if seen > max_entries:
                return {"files": sorted(set(files)), "escaping_links": sorted(set(links))}
            candidate = current / name
            if candidate.is_symlink() and _escapes(candidate, root):
                links.append(_rel(candidate, root))
                continue
            if _matches(name, SECRET_FILE_PATTERNS):
                from .secret_public_files import public_file
                if not public_file(candidate):
                    files.append(_rel(candidate, root))

    return {"files": sorted(set(files)), "escaping_links": sorted(set(links))}


def _escapes(path: Path, root: Path) -> bool:
    try:
        target = path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return True
    try:
        target.relative_to(root)
        return False
    except ValueError:
        return True


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def assert_clean(worktree: str | Path) -> list[str]:
    """Human-readable problems to fix before a worker runs here."""
    found = scan_worktree(worktree)
    problems = []
    for rel in found["files"]:
        problems.append(
            f"{rel} is a credential file inside a worker-accessible worktree; "
            "gitignore it and keep it out of the checkout"
        )
    for rel in found["escaping_links"]:
        problems.append(
            f"{rel} is a link that resolves outside the worktree; it defeats workspace "
            "confinement and must be removed"
        )
    return problems
