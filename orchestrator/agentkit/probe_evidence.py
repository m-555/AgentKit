"""Structured evidence for attempted writes denied by AgentKit guards."""
from __future__ import annotations

import json
import re


def guard_denial(output: str, capability: str, target: str) -> bool:
    """Correlate a denied tool result to the negative-control tool invocation."""
    attempted = set()
    file_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    tools = file_tools if capability == "prewrite_file_guard" else {"Bash", "PowerShell"}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        blocks = message.get("content") or [] if isinstance(message, dict) else []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (event.get("type") == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") in tools and _targets(block.get("input"), target, capability)
                    and isinstance(block.get("id"), str)):
                attempted.add(block["id"])
            if (event.get("type") == "user" and block.get("type") == "tool_result"
                    and block.get("tool_use_id") in attempted and block.get("is_error") is True):
                text = str(block.get("content", "")).lower()
                if "[agentkit]" in text and "blocked" in text and target.replace("\\", "/").rsplit("/", 1)[-1].lower() in text:
                    return True
    return False


def _normalize_display(value: str, *, casefold: bool = True) -> str:
    normalized = re.sub(r"/+", "/", value.replace("\\", "/"))
    return normalized.lower() if casefold else normalized


def _targets(tool_input, target: str, capability: str) -> bool:
    if not isinstance(tool_input, dict):
        return False
    if capability == "shell_guard":
        command = tool_input.get("command") or tool_input.get("cmd")
        return isinstance(command, str) and _write_attempt(command, target)
    target = _normalize_display(target)
    paths = [tool_input.get(key) for key in ("file_path", "path", "notebook_path")]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        paths.extend(edit.get("file_path") for edit in edits if isinstance(edit, dict))
    return any(isinstance(path, str) and _normalize_display(path) in {target, target.rsplit("/", 1)[-1]}
               for path in paths)


def _write_attempt(command: str, target: str, *, allow_relative: bool = True) -> bool:
    """Accept the probe's simple write primitives; opaque/chained commands are inconclusive."""
    # Codex wraps native PowerShell invocations and may escape inner path quotes.
    command = command.replace('\\"', '"')
    fold = bool(re.match(r"^[A-Za-z]:", target))
    command = _normalize_display(command, casefold=fold)
    target = _normalize_display(target, casefold=fold)
    if any(separator in command for separator in (";", "&&", "||", "\n")):
        return False
    wrapper = re.fullmatch(r'"[^"\n]*(?:powershell|pwsh)(?:\.exe)?"\s+(?:-noprofile\s+)?-command\s+"(.*)"', command)
    if wrapper:
        command = wrapper.group(1)
    # A relative canary path resolves in the tool's probe workspace.
    candidates = [target, target.rsplit("/", 1)[-1]] if allow_relative and "/" in target else [target]
    for candidate in candidates:
        token = re.escape(candidate)
        quoted = r"(?:['\"]" + token + r"['\"]|" + token + r")"
        set_content = (r"(?i:set-content\s+(?:-(?:literalpath|path)\s+)?)" + quoted
                       + r"(?i:\s+(?:-value\s+)?['\"]?breached['\"]?(?:\s+-nonewline)?)")
        redirect = r"(?i:(?:echo|printf)\s+['\"]?breached(?:/r)?(?:/n)?['\"]?\s*>\s*)" + quoted
        if re.fullmatch(set_content, command.strip()) or re.fullmatch(redirect, command.strip()):
            return True
    return False


def _mentions_target(text: str, target: str, fold: bool) -> bool:
    normalized = _normalize_display(text, casefold=fold)
    return re.search(r"(?<![A-Za-z0-9_/.-])" + re.escape(target)
                     + r"(?=$|[\s'\":])", normalized) is not None


def sandbox_denial(output: str, target: str) -> bool:
    """A failing shell result must belong to the seeded outside-write command."""
    attempted = set()
    fold = bool(re.match(r"^[A-Za-z]:", target))
    target = _normalize_display(target, casefold=fold)
    denial_texts = ("permission denied", "access is denied", "read-only file system", "operation not permitted", "unauthorizedaccessexception")
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            command = str(item.get("command", ""))
            raw_text = str(item.get("aggregated_output") or item.get("output") or "")
            text = raw_text.lower()
            if (event.get("type") == "item.completed" and _write_attempt(command, target, allow_relative=False)
                    and isinstance(item.get("exit_code"), int) and item["exit_code"] != 0
                    and _mentions_target(raw_text, target, fold)
                    and "[agentkit]" not in text
                    and any(reason in text for reason in denial_texts)):
                return True
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (event.get("type") == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") in {"Bash", "PowerShell"}):
                tool_input = block.get("input")
                command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
                if _write_attempt(command, target, allow_relative=False) and isinstance(block.get("id"), str):
                    attempted.add(block["id"])
            if (event.get("type") == "user" and block.get("type") == "tool_result"
                    and block.get("tool_use_id") in attempted and block.get("is_error") is True):
                raw_text = str(block.get("content", ""))
                text = raw_text.lower()
                # Native shells report errno, without necessarily naming the sandbox.
                # Require the exact attempted path and exclude our hook-denial channel.
                if (_mentions_target(raw_text, target, fold) and "[agentkit]" not in text
                        and any(reason in text for reason in denial_texts)):
                    return True
    return False


def authority_guard_denial(rows: list[dict], capability: str, target: str) -> bool:
    """Authority rows generated during this attempt bind Codex's omitted denied tools."""
    layer = "L3" if capability == "prewrite_file_guard" else "L4"
    channels = {"tool:apply_patch", "tool:Edit", "tool:Write", "tool:MultiEdit", "tool:NotebookEdit"} if layer == "L3" else {"shell"}
    normalized = _normalize_display(target)
    return any(row.get("layer") == layer and row.get("channel") in channels
               and _normalize_display(str(row.get("path", ""))) in {normalized, normalized.rsplit("/", 1)[-1]}
               for row in rows)
