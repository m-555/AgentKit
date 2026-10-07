"""`agentkit init` — onboarding an existing repository.

Adoption cost is the whole point of the plugin design: behavior lives in the
plugin, so a project only needs to declare *facts about itself*. This module
guesses as many of those facts as it safely can and leaves clearly-marked holes
where it cannot.

Nothing here overwrites an existing file unless `force` is set. A repository that
already has a good `AGENTS.md` keeps it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "plugins" / "agentkit" / "templates"
if not TEMPLATE_DIR.is_dir():
    TEMPLATE_DIR = Path(__file__).parent / "data" / "templates"


@dataclass
class Detection:
    stacks: list[str] = field(default_factory=list)
    gates: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Relative path to the interpreter the gates use, when one was found. Set
    #: only so `worktree_setup` can rebuild the same layout inside a worktree.
    venv_relpath: str = ""
    setup: list[str] = field(default_factory=list)


def detect(root: Path) -> Detection:
    found = Detection()

    has_py = any((root / n).exists() for n in ("pyproject.toml", "requirements.txt", "setup.py"))
    if has_py:
        found.stacks.append("python")
        py = "python"
        for venv in (".venv/Scripts/python.exe", ".venv/bin/python"):
            if (root / venv).exists():
                # Quoted, and with forward slashes. Both halves matter: bare
                # `.venv/Scripts/python.exe` is not executable by cmd.exe, which
                # reads the leading token up to the first `/` and reports
                # "'.venv' is not recognized"; and backslashes would not survive to
                # a POSIX shell if this committed config is ever used from one.
                # Quoting makes a single forward-slash path work in both.
                py = f'"{venv}"'
                found.venv_relpath = venv
                break
        test_cmd = f"{py} -m pytest -q"
        if (root / "pytest.ini").exists() or (root / "tests").is_dir():
            found.gates.setdefault("fast", []).append(f"{test_cmd} -x")
            found.gates.setdefault("full", []).append(test_cmd)
        if (root / ".ruff.toml").exists() or _mentions(root / "pyproject.toml", "ruff"):
            found.gates.setdefault("fast", []).append(f"{py} -m ruff check .")
        if _mentions(root / "pyproject.toml", "mypy") or (root / "mypy.ini").exists():
            found.gates.setdefault("types", []).append(f"{py} -m mypy .")

        if found.venv_relpath:
            # The gates above run `.venv/...`, and a worker's worktree never has
            # one. Rebuild the same layout there, or the worker fails a gate it was
            # never able to pass. Installs go through the shared package cache the
            # launcher exports, so this is a download once, not once per worktree.
            installer = f'"{found.venv_relpath}" -m pip install -q'
            found.setup = ["python -m venv .venv"]
            if (root / "requirements.txt").exists():
                found.setup.append(f"{installer} -r requirements.txt")
                for extra_req in ("requirements-dev.txt", "requirements-test.txt"):
                    if (root / extra_req).exists():
                        found.setup.append(f"{installer} -r {extra_req}")
            elif (root / "pyproject.toml").exists() or (root / "setup.py").exists():
                # The base install almost never carries pytest — test dependencies
                # live in an extra. Installing without it produces a worktree that
                # builds fine and then cannot run a single gate.
                extra = _test_extra(root / "pyproject.toml")
                target = f'".[{extra}]"' if extra else "."
                found.setup.append(f"{installer} -e {target}")
                if not extra:
                    found.notes.append(
                        "no dev/test extra found in pyproject.toml — if your test "
                        "tools are declared elsewhere, add them to `worktree_setup`, "
                        "or a worker's worktree will build but run no gate."
                    )
            found.notes.append(
                "worktree_setup was guessed from your layout — run `agentkit doctor` "
                "after editing it; a worker's worktree has no .venv of its own."
            )

    pkg = root / "package.json"
    if pkg.exists():
        found.stacks.append("node")
        scripts: dict[str, str] = {}
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts", {}) or {}
        except (json.JSONDecodeError, OSError):
            found.notes.append("package.json could not be parsed; gate commands not guessed")
        for name, level in (("test", "full"), ("lint", "fast"), ("typecheck", "types"),
                            ("build", "build")):
            if name in scripts:
                found.gates.setdefault(level, []).append(f"npm run {name}")
        if scripts:
            # `npm run` needs node_modules, which a worktree never inherits, for the
            # same reason .venv is not inherited.
            found.setup.append("npm ci" if (root / "package-lock.json").exists()
                               else "npm install")

    if list(root.glob("*.uproject")):
        found.stacks.append("unreal")
        found.notes.append(
            "Unreal project detected — set the Build.bat command for the `build` gate by hand."
        )

    for sub in ("apps", "packages", "services", "frontend", "backend"):
        if (root / sub).is_dir():
            found.notes.append(f"`{sub}/` present: consider a nested AGENTS.md there")
            break

    if not found.gates:
        found.notes.append("No gate commands detected — fill in `gates:` before running agents.")
    return found


#: Extra names that conventionally carry test tooling, best first.
_TEST_EXTRAS = ("dev", "test", "tests", "testing", "develop")


def _test_extra(pyproject: Path) -> str:
    """The optional-dependency group that most likely holds pytest.

    Read with a real TOML parser rather than a regex: `[project.optional-dependencies]`
    and `[dependency-groups]` both nest, and a wrong guess here produces a worktree
    that installs cleanly and then cannot run a single gate.
    """
    try:
        import tomllib
    except ModuleNotFoundError:            # pragma: no cover - Python < 3.11
        return ""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    extras = (data.get("project") or {}).get("optional-dependencies") or {}
    if not isinstance(extras, dict):
        return ""
    # Prefer a conventional name; otherwise take any extra that names pytest.
    for name in _TEST_EXTRAS:
        if name in extras:
            return name
    for name, requirements in extras.items():
        if any("pytest" in str(r) for r in (requirements or [])):
            return str(name)
    return ""


def _mentions(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _render_project_yaml(root: Path, found: Detection) -> str:
    lines = [
        "# AgentKit project facts. Everything stack-specific lives here, not in the framework.",
        f"name: {root.name}",
        f"worktree_root: {json.dumps(str(root.parent / ('wt-' + root.name)))}",
        f"stacks: [{', '.join(found.stacks) or 'unknown'}]",
        "",
        "# Commands every role runs. `fast` before a commit, `full` before a merge.",
        "gates:",
    ]
    if found.gates:
        for level in ("fast", "full", "types", "build"):
            if found.gates.get(level):
                items = ", ".join(json.dumps(c) for c in found.gates[level])
                lines.append(f"  {level}: [{items}]")
    else:
        lines += [
            '  fast: []   # e.g. ["python -m pytest -q -x"]',
            '  full: []   # e.g. ["python -m pytest -q", "npm test"]',
        ]
    lines += [
        "",
        "# Commands that build a worker's environment inside its own fresh worktree,",
        "# run once when that worktree is created and before its first gate.",
        "#",
        "# A worktree deliberately never inherits `.venv` or `node_modules`: sharing a",
        "# writable environment lets one worker's install corrupt another's. So whatever",
        "# your gates above need has to be built here, or every worker fails a gate it",
        "# was never able to pass. Package downloads use a shared cache, so this costs",
        "# disk per worktree but not bandwidth.",
        "worktree_setup:",
    ]
    if found.setup:
        lines += [f"  - {json.dumps(c)}" for c in found.setup]
    else:
        lines += [
            '  []   # e.g. ["python -m venv .venv", ".venv/bin/python -m pip install -e ."]'
        ]
    lines += [
        "",
        "# Files no task may edit without an explicit lease. Run `agentkit hotspots` to fill this in.",
        "hot_paths: []",
        "",
        "# Shared shapes. Only a CONTRACT_CHANGE task may modify these, and only before the",
        "# contract is frozen. Everyone else gets a read-only lease.",
        "contracts: []",
        "",
        "# Outputs that are regenerated rather than merged, and so are never audited",
        "# against a lease. Defaults already cover caches and build directories.",
        "generated: []",
        "",
        "# Shell commands the L4 guard trusts even though their write targets cannot be",
        "# proven from the command line. Every command under `gates:` is trusted already.",
        "allowlisted_commands: []",
        "",
        "# Where reviewed work is combined before it reaches main.",
        "integration_branch: integration",
        "",
        "budgets:",
        "  worker_usd: 3.00",
        "  integration_usd: 5.00",
        "",
        "# Coordinator/reviewer: Astra, then Opus, both high. Workers: Sol, Opus.",
        "# Sonnet needs an easy task; Qwen needs explicit easy research assignment.",
        "# Overrides apply to future sessions; an existing coordinator stays pinned.",
        "model_policy:",
        "  profiles:",
        "    # Omit model to follow the verified AgentKit catalog. Use pinned: true",
        "    # alongside an explicit model ID to keep a specific version.",
        "    astra: {effort: high, enabled: true}",
        "    sol: {effort: high, enabled: true}",
        "    opus: {effort: high, enabled: true}",
        "    sonnet: {effort: high, enabled: true}",
        "    qwen: {enabled: true}",
        "",
    ]
    from .environment_defaults import render
    lines += render(found)
    return "\n".join(lines)


def _render_tasks_yaml() -> str:
    return (
        "# Task specification — the desired graph, committed to git.\n"
        "# Runtime state (status, leases, attempts, spend) lives in .ai/tasks.db and never\n"
        "# appears here. Editing a task that is in flight moves it to NEEDS_REPLAN on the\n"
        "# next `agentkit reconcile`.\n"
        "#\n"
        "# tasks:\n"
        "#   - id: media-abstraction\n"
        "#     title: Extract the MediaProvider interface\n"
        "#     kind: DECOUPLE          # SAFE_PARALLEL | DEPENDENT | HOTSPOT | DECOUPLE\n"
        "#     role: decoupler         #   | CONTRACT_CHANGE | TEST_ONLY | RESEARCH\n"
        "#     expected_paths:\n"
        "#       write: [services/media/**]\n"
        "#       read:  [contracts/**]\n"
        "#   - id: provider-veo\n"
        "#     title: Add the veo provider\n"
        "#     depends_on: [media-abstraction]\n"
        "#     expected_paths:\n"
        "#       write: [services/media/providers/veo.py, tests/media/test_veo.py]\n"
        "tasks: []\n"
    )


def _render_agents_md(root: Path, found: Detection) -> str:
    gate_lines = []
    for level in ("fast", "full", "types", "build"):
        for command in found.gates.get(level, []):
            gate_lines.append(f"| `{level}` | `{command}` |")
    gate_table = "\n".join(gate_lines) or "| — | _declare these in `.ai/project.yaml`_ |"
    return f"""# {root.name} — agent guide

<!-- Written by `agentkit init`. Replace the TODOs; this file is read on every turn. -->

## What this project is

TODO: two or three sentences. What it does, who uses it, what it talks to.

## Working agreement

This repository is managed by **AgentKit**. Before editing anything:

1. Call `brief` (agentkit MCP) to get your task, scope and gates.
2. Stay inside your `owned_paths`. Edits outside them are blocked by a hook.
3. Need a file you do not own? Call `lease_request`. Never edit around the block.
4. Think the task itself is wrong? Call `graph_amend`.
5. Commit small logical milestones, and `checkpoint` before you stop.

## Gates

| Level | Command |
|---|---|
{gate_table}

Run `fast` before committing and `full` before requesting a merge.

## Structure

TODO: the map. For each major directory, one line on what belongs there.
Keep it accurate — this is the file that stops agents guessing.

| Task | Where |
|---|---|
| TODO | TODO |

## House rules

- TODO: naming, error handling, logging conventions.
- Do not edit `.ai/tasks.db` — it is AgentKit state, reachable through MCP tools only.
- Do not merge to `main`; that is the integrator's job.
"""


def _render_settings_json() -> str:
    settings = {
        "enabledPlugins": {"agentkit@agentkit-local": True},
        "permissions": {
            "deny": [
                "Edit(.ai/tasks.db)",
                "Read(.env)",
                "Edit(.env)",
            ],
            "ask": ["Bash(git merge *)", "Bash(git push *)"],
        },
        "worktree": {
            "symlinkDirectories": [],
            "baseRef": "head",
        },
    }
    return json.dumps(settings, indent=2) + "\n"


def _render_architecture_md(root: Path) -> str:
    return f"""# {root.name} — architecture for agents

<!-- The architect maintains this. It is the contract that makes parallel work safe. -->

## Module boundaries

TODO: one section per module. What it owns, what it must not know about.

## Contracts

TODO: the shapes crossing a boundary — API schemas, DB tables, generated types.
Changing one of these is always an architect task, never a feature task.

## Where new functionality goes

TODO: "a new provider goes in X and registers in Y" — the sentence that stops
five agents inventing five different places for the same thing.

## Known hotspots

Run `agentkit hotspots` and record the top entries here with a decoupling plan.
"""


def _append_gitignore(root: Path, entries: list[str]) -> list[str]:
    path = root / ".gitignore"
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="ignore")
    missing = [e for e in entries if e not in existing]
    if not missing:
        return []
    block = "\n# AgentKit state\n" + "\n".join(missing) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)
    return missing


def init(root: str | Path, *, force: bool = False) -> dict[str, list[str]]:
    root_path = Path(root).resolve()
    found = detect(root_path)
    created: list[str] = []
    skipped: list[str] = []

    def write(rel: str, content: str) -> None:
        target = root_path / rel
        if target.exists() and not force:
            skipped.append(rel)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(rel)

    write(".ai/project.yaml", _render_project_yaml(root_path, found))
    write(".ai/tasks.yaml", _render_tasks_yaml())
    write(".ai/architecture.md", _render_architecture_md(root_path))
    write("AGENTS.md", _render_agents_md(root_path, found))
    write("CLAUDE.md", "@AGENTS.md\n")
    write(".claude/settings.json", _render_settings_json())
    (root_path / ".ai" / "runtime").mkdir(parents=True, exist_ok=True)

    ignored = _append_gitignore(root_path, [".ai/tasks.db", ".ai/tasks.db-wal",
        ".ai/tasks.db-shm", ".ai/runtime/", ".ai/capabilities.json", ".ai/githooks/",
        ".claude/settings.local.json", ".codex/project.rules", ".codex/hooks.json"])
    return {
        "created": created,
        "skipped": skipped,
        "gitignore": ignored,
        "stacks": found.stacks,
        "notes": found.notes,
    }
