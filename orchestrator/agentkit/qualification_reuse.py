"""Reuse measured confinement only after proof that its own guard sources are identical."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import capabilities, db, runtime_identity


def unchanged(root, adapter, qualified_revision: str) -> dict:
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("Workers cannot certify their own confinement")
    if not re.fullmatch(r"[0-9a-f]{40}", qualified_revision):
        raise ValueError("An exact previously measured source commit is required")
    cached = capabilities.load_cache(root)
    prior = cached.get(adapter.name)
    install = adapter.detect()
    if not prior or not install or prior.version != install.version:
        raise ValueError("Measured matching runtime is required")
    for field in ("workspace_sandbox", "prewrite_file_guard", "shell_guard"):
        if not prior.has(field) or not prior.notes.get(field, "").startswith("functional v2:"):
            raise ValueError("Complete prior functional evidence is required")
    current = runtime_identity.identify(install)
    previous = dict(prior.installation)
    if {k: v for k, v in previous.items() if k != "enforcement"} != {
            k: v for k, v in current.items() if k != "enforcement"}:
        raise ValueError("Executable, host or transport changed; measure again")
    base = Path(runtime_identity.__file__).resolve().parent
    source_repo = base.parents[1]

    def historical(name):
        result = subprocess.run(["git", "show", f"{qualified_revision}:orchestrator/agentkit/{name}"],
                                cwd=source_repo, capture_output=True, timeout=10)
        if result.returncode:
            return None
        value = result.stdout
        present = (base / name).read_bytes() if (base / name).is_file() else b""
        if b"\r\n" in present and b"\n" not in present.replace(b"\r\n", b""):
            value = value.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return value

    old = runtime_identity.digest_sources(runtime_identity.LEGACY_NAMES, historical)
    if old != previous.get("enforcement"):
        raise ValueError("Revision does not reproduce the measured guard fingerprint")
    for name in runtime_identity.enforcement_names(adapter.name):
        original = historical(name)
        if original is None or not (base / name).is_file() or (base / name).read_bytes() != original:
            raise ValueError(f"Relevant guard changed: {name}; measure again")
    if runtime_identity.identify(adapter.detect()) != current:
        raise ValueError("Runtime changed during equivalence proof")
    prior.installation = current
    capabilities.save_cache(root, cached)
    evidence = {"provider": adapter.name, "qualified_revision": qualified_revision,
                "previous_fingerprint": old, "current_fingerprint": current["enforcement"],
                "model_calls": 0, "prior_probed_at": prior.probed_at}
    conn = db.connect(root)
    try:
        db.log_event(conn, None, "qualification_reused_unchanged_guards", detail=evidence)
    finally:
        conn.close()
    return evidence
