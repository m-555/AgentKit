"""Provider-specific external session identity; credentials still authorize the lease."""
from __future__ import annotations

import os


def reference(provider):
    names = {"codex": ("CODEX_THREAD_ID",),
             "claude-code": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
             "local-opencode": ("AGENTKIT_EXTERNAL_SESSION_REF",)}
    return next((os.environ[name] for name in names.get(provider, ("AGENTKIT_EXTERNAL_SESSION_REF",))
                 if os.environ.get(name)), "")


def require(lease):
    actual = reference(lease["provider"])
    if not actual or actual != lease["session_ref"]:
        raise PermissionError("Operation must originate from the attached provider/session")
    return actual
