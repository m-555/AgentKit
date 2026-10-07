"""Stage exact audited paths without forcing ignored, untracked files into Git."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .secrets import redact_text


def stage(work: Path, paths: list[str]) -> None:
    if not paths:
        return
    tracked = subprocess.run(
        ["git", "--literal-pathspecs", "ls-files", "-z", "--", *paths],
        cwd=work, capture_output=True, check=True, timeout=30,
    ).stdout.decode("utf-8").split("\0")
    known = set(tracked)
    # Git add can reject tracked files beneath an ignored parent. Updating
    # tracked paths works without force-adding ignored new files or secrets.
    for update in (True, False):
        selected = [p for p in paths if (p in known) == update]
        if not selected:
            continue
        command = ["git", "--literal-pathspecs", "add"]
        if update:
            command.append("-u")
        result = subprocess.run([*command, "--", *selected], cwd=work,
                                capture_output=True, timeout=30)
        if result.returncode:
            detail = redact_text(result.stderr.decode("utf-8", errors="replace"))
            raise ValueError("Host staging rejected audited paths: " + detail[-1500:])
