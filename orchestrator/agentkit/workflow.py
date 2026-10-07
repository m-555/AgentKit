"""Opt-in separate builder/tester workflow (`workflow.mode: separate-tasks`).

With the mode enabled in `.ai/project.yaml`, every worker task is one short,
exact-file assignment for one of four roles: builders write only source files,
testers write only test files against the implementation they depend on, and
every launch is a fresh session. With it disabled each function reproduces the
behaviour that existed before this module, so callers may use it unconditionally.

Nothing here truncates. A task or job intent that does not fit the limits is
refused with an explicit error so the plan is changed, never silently shortened.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import jobs

_MODE = "separate-tasks"
_BUILDERS = ("backend-builder", "frontend-builder")
_TESTERS = ("backend-tester", "frontend-tester")
_DEFAULTS = {
    "max_write_files": 3,
    "max_read_files": 6,
    "max_task_chars": 1200,
    "max_context_chars": 16000,
}
_GLOB_CHARS = frozenset("*?[]{}")
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e"})
_ROLE_TEXT = {
    "builder": (
        "You are the {area} builder for this one short task. Write only the exact source files "
        "you own. Do not delegate or launch sessions. Do not write, edit or run tests, and do not review: an independent tester "
        "verifies your commit. Use host task_commit before the declared gate_run, then report REVIEW "
        "with the gate output."
    ),
    "tester": (
        "You are the {area} tester for this one short task. Write and run focused tests only in "
        "the exact test files you own, against the implementation this task depends on. Do not delegate or launch sessions. Never "
        "edit source files: when a test exposes a defect, commit the test and report the failure "
        "with its output instead of fixing it. Do not run broad full suites."
    ),
}


def _settings(project: Any) -> dict[str, Any]:
    raw = getattr(project, "raw", None)
    section = raw.get("workflow") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _limit(project: Any, key: str) -> int:
    value = _settings(project).get(key, _DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"workflow.{key} must be a positive integer, not {value!r}")
    return value


def _field(task: Any, key: str) -> Any:
    """Read a task given as a database dict or as a `TaskSpec`."""
    return task.get(key) if isinstance(task, dict) else getattr(task, key, None)


def _strings(task: Any, key: str) -> list[str]:
    value = _field(task, key)
    if isinstance(value, str):  # an undecoded JSON column
        value = json.loads(value or "[]")
    return [str(item) for item in value or []]


def _decode(value: Any, label: str, problems: list[str]) -> list[str]:
    """`value` as a list of strings, decoding a JSON column; anything else is a reported problem."""
    if isinstance(value, str):  # an undecoded JSON column
        try:
            value = json.loads(value or "[]")
        except ValueError:
            problems.append(f"{label} is not a JSON list: {value!r}")
            return []
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        problems.append(f"{label} must be a list of strings, not {value!r}")
        return []
    return list(value)


def _declared_paths(task: Any, problems: list[str]) -> tuple[list[str], list[str]]:
    """Write and read paths from runtime `expected_write`/`expected_read` or `TaskSpec.to_dict()`."""
    nested = _field(task, "expected_paths")
    if nested is not None and not isinstance(nested, dict):
        problems.append(f"expected_paths must be a mapping, not {nested!r}")
        nested = None
    nested = nested or {}
    unknown = sorted(str(key) for key in set(nested) - {"write", "read"})
    if unknown:
        problems.append(
            f"expected_paths supports only write and read, not {', '.join(unknown)}"
        )
    declared: list[list[str]] = []
    for label in ("write", "read"):
        flat = _field(task, f"expected_{label}")
        paths = _decode(flat, f"expected_{label}", problems)
        if label in nested:
            inner = _decode(nested[label], f"expected_paths.{label}", problems)
            if flat is not None and sorted(inner) != sorted(paths):
                problems.append(f"expected_{label} and expected_paths.{label} disagree")
            paths = inner
        declared.append(paths)
    return declared[0], declared[1]


def _is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    name = parts[-1]
    stem = name.split(".", 1)[0]
    return (
        any(part in _TEST_DIRS for part in parts[:-1])
        or name == "conftest.py"
        or stem.startswith("test_")
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )


def _inexact(project: Any, path: str) -> str | None:
    """Why `path` is not one exact file, or None when it is."""
    if path.strip() in ("", ".", "./"):
        return f"empty path {path!r}"
    clean = path.replace("\\", "/")
    if clean.startswith("/") or ":" in clean or ".." in clean.split("/"):
        return f"non-relative or escaping path {path!r}"
    if _GLOB_CHARS & set(path):
        return f"glob {path!r}"
    root = getattr(project, "root", None)
    if path.endswith(("/", "\\")) or (root is not None and (Path(root) / path).is_dir()):
        return f"directory {path!r}"
    return None


def enabled(project: Any) -> bool:
    """True only when `.ai/project.yaml` sets `workflow.mode: separate-tasks`."""
    return _settings(project).get("mode") == _MODE


def validate_task(project: Any, task: Any) -> None:
    """Refuse a task that breaks the separate-tasks rules; a no-op when disabled.

    Every problem is reported in one error so a single replan can fix them all.
    Paths come from runtime `expected_write`/`expected_read` or the
    `expected_paths` mapping of `TaskSpec.to_dict()`. A RESEARCH task declaring
    no write paths is read-only: it needs no file to write and no tester shape,
    but every other limit still applies.
    """
    if not enabled(project):
        return
    problems: list[str] = []
    role = str(_field(task, "role") or "")
    kind = str(_field(task, "kind") or "SAFE_PARALLEL")
    writes, reads = _declared_paths(task, problems)
    read_only = kind == "RESEARCH" and not writes
    if role not in (*_BUILDERS, *_TESTERS):
        problems.append(f"role {role!r} is not one of {', '.join((*_BUILDERS, *_TESTERS))}")
    if kind == "RESEARCH" and writes:
        problems.append(f"a read-only RESEARCH task may not write: {', '.join(writes)}")
    elif not writes and not read_only:
        problems.append("no exact file to write")
    for label, paths in (("write", writes), ("read", reads)):
        limit = _limit(project, f"max_{label}_files")
        if len(paths) > limit:
            problems.append(f"{len(paths)} {label} files exceed max_{label}_files {limit}")
        for path in paths:
            issue = _inexact(project, path)
            if issue:
                problems.append(f"{label} path is {issue}, not an exact file")
    if role in _BUILDERS:
        tests = [path for path in writes if _is_test_file(path)]
        if tests:
            problems.append(f"a builder may not write test files: {', '.join(tests)}")
        if kind == "TEST_ONLY":
            problems.append("a builder task cannot be TEST_ONLY")
    elif role in _TESTERS and not read_only:
        sources = [path for path in writes if not _is_test_file(path)]
        if sources:
            problems.append(f"a tester may not write source files: {', '.join(sources)}")
        if kind != "TEST_ONLY":
            problems.append(f"a tester task must be TEST_ONLY, not {kind}")
        if not _strings(task, "depends_on"):
            problems.append("a tester task must depend on the implementation it verifies")
    size = (
        len(str(_field(task, "title") or ""))
        + len(str(_field(task, "description") or ""))
        + sum(len(item) for item in _strings(task, "acceptance"))
    )
    limit = _limit(project, "max_task_chars")
    if size > limit:
        problems.append(f"assignment is {size} chars, over max_task_chars {limit}")
    gate = _field(task, "gate_level")
    if gate:
        try:
            validate_gate(project, task, str(gate))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        name = _field(task, "spec_id") or _field(task, "id") or _field(task, "title") or "task"
        raise ValueError(
            f"separate-tasks workflow refuses {name}: "
            + "; ".join(problems)
            + ". Split or replan it; nothing is truncated."
        )


def role_text(project: Any, role: str) -> str | None:
    """Concise instructions for a builder or tester; None outside enabled worker roles."""
    if not enabled(project) or role not in (*_BUILDERS, *_TESTERS):
        return None
    area, duty = role.split("-", 1)
    return (f"## Role: {role}\n" + _ROLE_TEXT[duty].format(area=area)
            + " Use AgentKit MCP audit_diff for scoped inspection, task_commit for host Git "
              "commits before gate_run, and gate_run for declared checks. Never invoke Git "
              "through a worker shell, including git diff/status. This applies after provider "
              "handoff too. Stop and checkpoint any denied tool; do not try alternatives.")


def job_context(project: Any, job_id: str | None, task: Any) -> str:
    """Job memory for a worker prompt.

    Disabled: the full durable packet, exactly as before. Enabled: a JSON object
    holding the job id, revision, all user requests and acceptance. Operational logs are excluded. An
    intent larger than `max_context_chars` is refused for replanning, never cut.
    """
    if not job_id:
        return ""
    if not enabled(project):
        return jobs.packet(project.root, job_id)
    owner = _field(task, "job_id")
    if owner and owner != job_id:
        raise ValueError(f"task belongs to job {owner!r}, not {job_id!r}")
    job = jobs.load(project.root, job_id)
    requests = job.get("requests") or []
    text = json.dumps(
        {
            "id": job["id"],
            "revision": job.get("revision"),
            "requests": requests,
            "acceptance": job.get("acceptance") or [],
        },
        ensure_ascii=False,
    )
    limit = _limit(project, "max_context_chars")
    if len(text) > limit:
        raise ValueError(
            f"replan required: job {job_id} context is {len(text)} chars, over max_context_chars "
            f"{limit}; shorten the request or acceptance with a job correction. Nothing is truncated."
        )
    return text


def resume_token(project: Any, task: Any, same_session: bool) -> str | None:
    """The provider session to resume, or None to launch fresh.

    Enabled mode always launches fresh, even when retrying the same task on the
    same model. Otherwise the existing token is reused iff `same_session`.
    """
    if enabled(project) or not same_session:
        return None
    return _field(task, "session_token")


def validate_prompt(project: Any, prompt: str) -> None:
    """Reject an oversized assembled packet before any model request."""
    if enabled(project) and len(prompt) > _limit(project, "max_context_chars"):
        raise ValueError(
            "replan required: assembled worker context exceeds max_context_chars; nothing is truncated"
        )


def validate_gate(project: Any, task: Any, level: str) -> None:
    """Workers run their task check; combined verification belongs to the manager."""
    if not enabled(project):
        return
    if level != _field(task, "gate_level"):
        raise ValueError("separate-tasks workers may run only their assigned gate")
    if _field(task, "role") in _BUILDERS:
        import re

        test_command = re.compile(
            r"\b(?:pytest|jest|vitest|ctest|nosetests)\b|\b(?:npm|pnpm|yarn|dotnet|cargo|go)\s+(?:run\s+)?test\b",
            re.I,
        )
        if any(test_command.search(command) for command in project.gate(level)):
            raise ValueError(
                "builder gate runs tests; configure a source-only gate and independent tester"
            )


def validate_launch_context(conn, project, task, prompt, paths, worktree):
    """Account for the brief delivered next, not just the initial launch text."""
    if not enabled(project):
        return
    from . import briefs
    brief = briefs.build(conn, project, int(task["id"]))
    if brief is None:
        raise ValueError("task disappeared before launch context validation")
    brief["owned_paths"] = paths
    brief["task"]["worktree"] = str(worktree)
    validate_prompt(project, prompt + "\n" + briefs.render(brief))
