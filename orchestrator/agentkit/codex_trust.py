"""Session-only trust for the exact AgentKit hook; no global trust bypass."""
from __future__ import annotations

import json
from pathlib import Path

from .adapters.availability import rpc


def _hooks(value):
    if isinstance(value, list):
        for child in value:
            yield from _hooks(child)
    elif isinstance(value, dict):
        if "currentHash" in value and "eventName" in value:
            yield value
        else:
            for child in value.values():
                yield from _hooks(child)


def reviewed_flags(binary: str, worktree: Path, command: str) -> list[str]:
    # Review is restricted to our shipped guard entrypoint and this exact definition.
    if not command.endswith(" -I -m agentkit.hooks_cli codex-pre-tool-use") and not command.endswith(" -I -m agentkit.hooks_cli codex-pre-tool-use; exit $LASTEXITCODE"):
        raise ValueError("Only the shipped AgentKit guard may receive session trust")
    definition = ('hooks.PreToolUse=[{matcher="^(apply_patch|Bash)$",hooks=[{type="command",command='
                  + json.dumps(command) + ',timeout=20}]}]')
    flags = ["-c", definition]
    parameters = {"cwds": [str(worktree)]}
    inventory = rpc(binary, "hooks/list", params=parameters, flags=flags, cwd=worktree)
    owned = [h for h in _hooks(inventory) if h.get("command") == command and h.get("source") == "sessionFlags"
             and h.get("matcher") == "^(apply_patch|Bash)$"
             and str(h.get("eventName", "")).replace("_", "").casefold() == "pretooluse"]
    if len(owned) != 1:
        raise ValueError("Exact AgentKit hook is missing or ambiguous; launch withheld")
    hook = owned[0]
    key, digest = hook.get("key"), hook.get("currentHash")
    if not isinstance(key, str) or not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("Codex did not report an exact hook identity")
    flags += ["-c", 'hooks.state={' + json.dumps(key) + '={trusted_hash=' + json.dumps(digest) + '}}']
    verified = [h for h in _hooks(rpc(binary, "hooks/list", params=parameters, flags=flags, cwd=worktree))
                if h.get("key") == key and h.get("command") == command and h.get("currentHash") == digest]
    if len(verified) != 1 or verified[0].get("trustStatus") != "trusted" or verified[0].get("enabled") is not True:
        raise ValueError("Scoped hook trust preflight failed; launch withheld")
    return flags
