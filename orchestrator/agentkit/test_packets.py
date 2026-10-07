"""Deterministic builder-to-tester packets; no planning/model call at handoff."""
from __future__ import annotations

import json
from pathlib import Path

from . import db, repo, workflow
from .paths import normalize


def _values(task, key):
    value = task.get(key, []) if isinstance(task, dict) else getattr(task, key, [])
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return list(value or [])


def _value(task, key):
    return task.get(key) if isinstance(task, dict) else getattr(task, key, None)


def is_tester(project, task):
    return (workflow.enabled(project) and _value(task, "kind") == "TEST_ONLY"
            and _value(task, "role") in ("backend-tester", "frontend-tester"))


def dependencies(project, task, definitions):
    """Validate the declared pairing without creating or widening task scopes."""
    if not is_tester(project, task):
        return []
    expected_role = str(_value(task, "role")).replace("tester", "builder")
    builders = []
    for name in _values(task, "depends_on"):
        matches = [item for item in definitions
                   if str(_value(item, "spec_id")) == str(name)
                   or str(_value(item, "id")) == str(name)]
        if len(matches) != 1:
            raise ValueError(f"tester dependency {name!r} is missing or ambiguous")
        dependency = matches[0]
        if _value(dependency, "job_id") != _value(task, "job_id"):
            raise ValueError("tester dependency belongs to another job")
        if _value(dependency, "role") != expected_role:
            continue  # Other prerequisites may be tests, contracts or research.
        paths = {normalize(path) for path in _values(dependency, "expected_write")}
        if not paths or _value(dependency, "kind") == "RESEARCH":
            continue
        missing = paths - {normalize(path) for path in _values(task, "expected_read")}
        if missing:
            raise ValueError("tester read scope must include implementation files: "
                             + ", ".join(sorted(missing)))
        builders.append(dependency)
    if not builders:
        raise ValueError(f"tester must depend on a source-writing {expected_role}")
    return builders


def build(conn, project, task):
    """Small metadata packet; sources are read from the tester's own checkout."""
    if not is_tester(project, task):
        return []
    builders = dependencies(project, task, db.list_tasks(conn))
    result = []
    for builder in builders:
        review = conn.execute(
            "SELECT head_sha,verdict FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (builder["id"],),
        ).fetchone()
        head = review["head_sha"] if review and review["verdict"] == "PASS" else None
        gate = db.cached_gate(conn, builder["id"], builder["gate_level"], head) if head else None
        ready = builder["status"] == "DONE" and bool(gate and gate["passed"])
        result.append({
            "task_id": builder["id"], "spec_id": builder["spec_id"],
            "state": "ready" if ready else "waiting",
            "commit": head if ready else None,
            "source_files": sorted({normalize(path) for path in _values(builder, "expected_write")}),
            "acceptance": _values(builder, "acceptance"),
            "gate": builder["gate_level"], "gate_passed": ready,
        })
    return result


def verify_checkout(conn, project, task, worktree):
    """Refuse stale/missing implementation before a tester session spends tokens."""
    if not is_tester(project, task):
        return
    packet = build(conn, project, task)
    if any(item["state"] != "ready" for item in packet):
        raise ValueError("tester awaits merged implementation and its current approval/gate")
    current = repo.head_commit(worktree)
    if not current:
        raise ValueError("tester checkout has no current commit")
    for item in packet:
        approved = item["commit"]
        if not repo.is_ancestor(worktree, approved, current):
            raise ValueError("tester checkout does not contain its approved implementation")
        for path in item["source_files"]:
            # Literal paths, not Git pathspec globs. Tree entries include modes as
            # well as object IDs, so symlink/type changes also require replanning.
            args = ["ls-tree", "-z"]
            old = repo._git([*args, approved, "--", ":(literal)" + path], worktree, strict=True)
            now = repo._git([*args, current, "--", ":(literal)" + path], worktree, strict=True)
            if old != now:
                raise ValueError(f"implementation file changed after approval: {path}; replan tester")
            candidate = Path(worktree) / path
            if not now and (candidate.exists() or candidate.is_symlink()):
                raise ValueError(f"deleted implementation file was recreated: {path}")
            changes = repo._git(
                ["status", "--porcelain", "-z", "--", ":(literal)" + path], worktree, strict=True,
            )
            if changes:
                raise ValueError(f"implementation file has uncommitted changes: {path}")


def render(packet):
    if not packet:
        return []
    lines = ["", "## Implementation handed off by AgentKit",
             "Read only the declared source inputs in your own checkout. Write behavioral tests",
             "for the criteria below; cover failures and boundary cases. Do not repair source.",
             "No builder conversation or repeated planning call is needed."]
    for item in packet:
        lines.append(f"- Builder task {item['task_id']} ({item['spec_id']}): {item['state']}")
        if item["commit"]:
            lines.append(f"  Approved commit: {item['commit']}; gate {item['gate']}: PASS")
        lines += [f"  Source: {path}" for path in item["source_files"]]
        lines += [f"  Required behavior: {criterion}" for criterion in item["acceptance"]]
    return lines
