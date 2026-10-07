"""Deterministic evidence of index and dirty bytes for safe continuation."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def fingerprint(worktree: str | Path) -> str:
    root = Path(worktree)
    digest = hashlib.sha256()
    def git(args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=60,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if result.returncode:
            raise ValueError("cannot fingerprint preserved worktree")
        return result.stdout
    digest.update(git(["diff", "--binary", "--no-ext-diff", "--no-textconv", "--cached", "HEAD", "--"]))
    digest.update(git(["diff", "--binary", "--no-ext-diff", "--no-textconv", "--",]))
    names = set(git(["diff", "--name-only", "-z", "HEAD", "--"]).split(b"\0"))
    names.update(git(["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0"))
    for name in sorted(n for n in names if n):
        path = root / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_symlink():
            digest.update(b"symlink:" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"file:")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
        elif path.exists():
            digest.update(b"non-file")
        else:
            digest.update(b"deleted")
        digest.update(b"\0")
    return digest.hexdigest()
