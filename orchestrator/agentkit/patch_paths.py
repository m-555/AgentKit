"""Conservative target extraction for Codex's apply_patch command payload.

Unknown syntax is refused rather than guessed. Sources and destinations of a
move both mutate files. This leaf imports no hook/adapter modules.
"""
from __future__ import annotations


def patch_targets(command: str) -> list[str]:
    if not isinstance(command, str) or len(command) > 8 * 1024 * 1024:
        raise ValueError("Missing or oversized apply_patch command")
    lines = command.strip().splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Unknown apply_patch envelope; use a direct patch")
    targets: list[str] = []
    operation = ""
    can_move = False
    for line in lines[1:-1]:
        marker = line.strip()
        header = next((name for name in ("Add File", "Update File", "Delete File", "Move to")
                       if marker.startswith(f"*** {name}: ")), None)
        if header:
            raw = marker[len(f"*** {header}: "):]
            if not raw or raw != raw.strip() or any(ord(char) < 32 for char in raw):
                raise ValueError("Malformed apply_patch target")
            if header == "Move to":
                if operation != "Update File" or not can_move:
                    raise ValueError("Move to must immediately follow Update File")
                can_move = False
            else:
                operation, can_move = header, header == "Update File"
            targets.append(raw)
            continue
        can_move = False
        if not operation or operation == "Delete File":
            raise ValueError("Unexpected apply_patch content")
        if operation == "Add File" and not line.startswith("+"):
            raise ValueError("Malformed Add File content")
        if operation == "Update File" and not (
                line.startswith((" ", "+", "-", "@@")) or marker == "*** End of File"):
            raise ValueError("Unknown Update File content")
    if not targets:
        raise ValueError("No apply_patch file targets")
    return list(dict.fromkeys(targets))
