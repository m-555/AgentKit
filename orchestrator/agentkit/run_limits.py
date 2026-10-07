"""Provider-neutral worker bounds. No model calls, content inference or quotas."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

DEFAULTS = {
    "max_turns": 16,
    "max_tool_calls": 32,
    "max_runtime_seconds": 900,
    "max_output_tokens": 20000,
}


def limits(project) -> dict[str, int]:
    if project is None:
        return {}
    from .workflow import enabled

    declared = getattr(project, "raw", {}).get("execution_limits", {})
    if not isinstance(declared, dict):
        raise ValueError("execution_limits must be a mapping")
    result = dict(DEFAULTS) if enabled(project) else {}
    for name, value in declared.items():
        if name not in DEFAULTS or type(value) is not int or value < 1:
            raise ValueError(
                f"execution_limits.{name} must be a positive integer and known limit"
            )
        result[name] = value
    return result


@dataclass
class Meter:
    """Track public identifiers/usage only; never retain tool arguments or reasoning."""

    messages: dict = field(default_factory=dict)
    tools: set = field(default_factory=set)
    turns: int | None = None
    output: int = 0
    stop_reason: str = ""

    def observe(self, line: str) -> None:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") or {}
            if not isinstance(message, dict):
                return
            identifier = message.get("id")
            usage = message.get("usage") or {}
            output = usage.get("output_tokens") if isinstance(usage, dict) else None
            if isinstance(identifier, str) and type(output) is int and output >= 0:
                self.messages[identifier] = max(self.messages.get(identifier, 0), output)
                self.output = max(self.output, sum(self.messages.values()))
            for block in message.get("content") or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("id"), str)
                ):
                    self.tools.add(block["id"])
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            if (
                isinstance(item, dict)
                and item.get("type")
                in ("command_execution", "mcp_tool_call", "web_search", "file_change")
                and isinstance(item.get("id"), str)
            ):
                self.tools.add(item["id"])
        elif kind in ("result", "turn.completed", "step_finish"):
            turns = event.get("num_turns")
            if type(turns) is int and turns >= 0:
                self.turns = turns
            usage = event.get("usage") or {}
            output = usage.get("output_tokens") if isinstance(usage, dict) else None
            if type(output) is int and output >= 0:
                self.output = max(self.output, output)
            if event.get("subtype") in ("error_max_turns", "error_max_budget_usd"):
                self.stop_reason = event["subtype"]

    def exceeded(self, budget: dict[str, int], elapsed: float) -> str:
        values = {
            "max_tool_calls": len(self.tools),
            "max_output_tokens": self.output,
            "max_runtime_seconds": elapsed,
            "max_turns": self.turns,
        }
        for name, ceiling in budget.items():
            value = values[name]
            if value is not None and value >= ceiling:
                return f"{name} reached ({value} / {ceiling}); work preserved for manager inspection"
        return ""
