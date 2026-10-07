"""Running a project's own checks.

The framework never hardcodes `pytest` or `npm test`. A project declares its
commands in `.ai/project.yaml` under `gates:`, and every role â€” reviewer,
integrator, the `Stop` verifier hook â€” runs the same declared commands. That is
the whole mechanism by which one framework serves Python, React and C++ repos.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ProjectConfig

DEFAULT_TIMEOUT = 1800
_TAIL = 4000


@dataclass
class CommandResult:
    command: str
    exit_code: int
    duration_s: float
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class GateResult:
    level: str
    passed: bool
    results: list[CommandResult] = field(default_factory=list)
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "passed": self.passed,
            "skipped_reason": self.skipped_reason,
            "commands": [
                {
                    "command": r.command,
                    "exit_code": r.exit_code,
                    "duration_s": round(r.duration_s, 1),
                    "ok": r.ok,
                    "output_tail": r.output_tail,
                }
                for r in self.results
            ],
        }

    def summary(self) -> str:
        if self.skipped_reason:
            return f"gate `{self.level}` skipped: {self.skipped_reason}"
        failed = [r for r in self.results if not r.ok]
        if not failed:
            return f"gate `{self.level}` passed ({len(self.results)} command(s))"
        lines = [f"gate `{self.level}` FAILED"]
        for r in failed:
            lines.append(f"  $ {r.command}  -> exit {r.exit_code}")
            tail = r.output_tail.strip().splitlines()[-25:]
            lines.extend(f"    {line}" for line in tail)
        return "\n".join(lines)


def _run_one(command: str, cwd: Path, timeout: int, *, env=None) -> CommandResult:
    import time

    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            **({"env": env} if env is not None else {}),
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        combined = f"timed out after {timeout}s"
        code = 124
    except OSError as exc:
        combined = f"failed to launch: {exc}"
        code = 127
    return CommandResult(
        command=command,
        exit_code=code,
        duration_s=time.monotonic() - started,
        output_tail=combined[-_TAIL:],
    )


def run_gate(
    project: ProjectConfig,
    level: str,
    *,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    stop_on_failure: bool = True,
) -> GateResult:
    commands = project.gate(level)
    if not commands:
        return GateResult(
            level=level,
            passed=False,
            skipped_reason=f"no `{level}` gate declared in .ai/project.yaml",
        )
    workdir = Path(cwd or project.root)
    results: list[CommandResult] = []
    for command in commands:
        from .playwright_gate import prepared
        try:
            with prepared(project, workdir, command) as private_command:
                result = _run_one(private_command, workdir, timeout)
        except (ValueError, OSError, KeyError) as error:
            result = CommandResult(command, 1, 0, "Browser preparation refused: " + str(error))
        results.append(result)
        if not result.ok and stop_on_failure:
            break
    return GateResult(level=level, passed=all(r.ok for r in results), results=results)


def run_worktree_setup(
    project: ProjectConfig,
    cwd: str | Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> GateResult:
    """Build a fresh worktree's environment before its first gate runs.

    Modelled as a gate result so the caller reports a failure the same way, and so
    a setup failure is as visible as a test failure â€” it has the same consequence.
    """
    commands = project.worktree_setup
    if not commands:
        return GateResult(
            level="worktree_setup", passed=True,
            skipped_reason="no `worktree_setup` declared in .ai/project.yaml",
        )
    workdir = Path(cwd)
    results: list[CommandResult] = []
    for command in commands:
        result = _run_one(command, workdir, timeout)
        results.append(result)
        if not result.ok:
            break
    return GateResult(
        level="worktree_setup", passed=all(r.ok for r in results), results=results
    )


#: Directory names that exist only in the main checkout, never in a worker's
#: worktree. Kept in step with `worktrees.ENV_DIRS`, which is the policy itself.
_ENV_DIR_NAMES = (".venv", "venv", "node_modules", "vendor", "target")


def unrunnable_in_worktree(project: ProjectConfig) -> list[tuple[str, str, str]]:
    """Gate commands that cannot possibly succeed inside a worker's worktree.

    Returns `(level, command, reason)` for each. This exists because the failure it
    detects is invisible until a worker hits it: `agentkit init` guesses a gate
    pointing at `.venv`, worktrees deliberately never contain one, and the worker
    then fails a gate it had no way to pass â€” burning an attempt each time, three
    of which push a healthy task to NEEDS_REPLAN.
    """
    if project.worktree_setup:
        return []                      # the environment is built there on purpose
    problems: list[tuple[str, str, str]] = []
    for level, commands in project.gates.items():
        for command in commands:
            normalised = command.replace("\\", "/")
            for name in _ENV_DIR_NAMES:
                if f"{name}/" in normalised:
                    problems.append((
                        level, command,
                        f"references `{name}/`, which a worker's worktree never has",
                    ))
                    break
    return problems


def describe_gates(project: ProjectConfig) -> str:
    if not project.gates:
        return "No gates declared in .ai/project.yaml."
    lines = []
    for level, commands in project.gates.items():
        lines.append(f"{level}:")
        lines.extend(f"  $ {c}" for c in commands)
    return "\n".join(lines)


def looks_like_shell_list(value: str) -> bool:
    """Sanity check used by `agentkit init` when guessing gate commands."""
    try:
        return bool(shlex.split(value))
    except ValueError:
        return False
