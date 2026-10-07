"""Recognize public configuration templates and certificate-only PEM files."""
from __future__ import annotations

import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

PLACEHOLDERS = {"", "your_key_here", "your-key-here", "your_api_key_here", "changeme", "change-me", "replace-me"}


def public_file(path: Path) -> bool:
    if path.name.lower() not in {".env.example", ".env.sample", ".env.template"} and path.suffix.lower() != ".pem":
        return False
    try:
        if path.stat().st_size > 2_000_000:
            return False
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if path.suffix.lower() == ".pem":
        from .public_ca_bundle import public_bundle
        return _certificates_only(content) or public_bundle(path, content)
    from .secrets import _PATTERNS, _is_secret_name
    for name, pattern in _PATTERNS:
        if name == "assignment":
            continue
        for finding in pattern.finditer(content):
            if name == "url-credentials" and _local_demo_url(content[finding.start():].split()[0]):
                continue
            return False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            return False
        name, value = match.groups()
        value = value.split(" #", 1)[0].strip().strip("\"'")
        if not _is_secret_name(name):
            continue
        if (name.endswith("_TOKENS") or "_TOKENS_" in name) and value.isdecimal():
            continue
        if value.lower() not in PLACEHOLDERS:
            return False
    return True


def _certificates_only(content: str) -> bool:
    stripped = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    pattern = re.compile(r"-----BEGIN CERTIFICATE-----\s*[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")
    blocks = pattern.findall(stripped)
    if not blocks or pattern.sub("", stripped).strip():
        return False
    try:
        for block in blocks:
            ssl.PEM_cert_to_DER_cert(block)
    except (ValueError, ssl.SSLError):
        return False
    return True


def _local_demo_url(value: str) -> bool:
    """A local database demo uses its database name as both user and password."""
    try:
        parsed = urlsplit(value.strip("\"'"))
        database = parsed.path.strip("/")
        return bool(parsed.scheme in {"postgresql", "postgres", "mysql"}
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    and database and (parsed.username == parsed.password == database
                        or (parsed.username in {"postgres", "user", "username"}
                            and parsed.password in {"password", "your_password_here"}))
                    and not parsed.query and not parsed.fragment)
    except ValueError:
        return False
