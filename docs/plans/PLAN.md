> Historical design: use [current documentation](../README.md) and the installed
> source for present behavior. This plan is reference material, not a worker brief.

# AgentKit — Multi-Agent Development Framework
### Plan v2 — optimized against verified Claude Code + Codex capabilities

> Supersedes `multi_agent_codex_claude_plan.md` (kept for diffing).
> v1 was architecturally sound but rested on a 2024-era assumption: that the agent CLIs are
> dumb subprocesses and every coordination mechanism must be hand-built. That is no longer
> true. Both CLIs now ship worktrees, plugins, marketplaces, hooks, MCP, background agents,
> session fork/resume, structured output and budget caps. **v2 keeps v1's engineering
> discipline and deletes roughly 60% of its plumbing.**

> **New to these terms?** Read [GLOSSARY.md](../GLOSSARY.md) first — every word used here
> (hook, plugin, MCP, worktree, lease, gate, seam…) explained in plain English.

---

## 0. Verified environment (checked on this machine, 2026-09-13)

| Component | Status | Notes |
|---|---|---|
| Claude Code | **2.1.167** (CLI) / **2.1.270** (VS Code ext) | Extension bundles `resources/native-binary/claude.exe` — *same engine*, different UI |
| Codex CLI | **0.154.0-alpha.6.1** | Bundled in the ChatGPT VS Code extension: `~/.vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe` |
| Codex on PATH | **not on PATH** | Must be fixed before any orchestration — see §12 Phase 0 |
| git | 2.51.0 | worktrees fine |
| Python / uv | 3.11.9 / uv 0.11.19 | orchestrator runtime |
| Node | 22.14.0 | `@anthropic-ai/claude-agent-sdk` already installed globally |
| Other agents in use | OpenCode, Aider, Jules, Gemini | the framework must stay **multi-vendor**, not Claude-only |

### Capabilities confirmed present (these replace hand-built v1 machinery)

**Claude Code** — `--worktree`; `claude agents` (background agent daemon + panel); plugins with
marketplaces and `plugin tag` releases; **33 hook events × 5 hook types**; `--session-id` /
`--resume` / `--fork-session`; `--output-format stream-json`; `--include-hook-events`;
`--json-schema` (structured output); `--max-budget-usd`; `--agents` (inline JSON agents);
`--effort`; `--add-dir`; `--settings`; `--mcp-config` / `--strict-mcp-config`; `--plugin-dir`;
`--append-system-prompt`; permissions `allow`/`deny`/`ask` + `defaultMode` +
`additionalDirectories` + `blockReadsOutsideWorkingDirectories`; and
`worktree.symlinkDirectories` / `sparsePaths` / `baseRef`.

**Codex** — `exec --json` (JSONL event stream); `exec resume` / `exec fork`; `--worktree`;
`--cd`; `--add-dir`; `--output-schema` (typed final message); `-o/--output-last-message`;
profiles (`-p` layers `$CODEX_HOME/<name>.config.toml`); sandbox modes
(`read-only` | `workspace-write` | `danger-full-access`); `--ask-for-approval`; MCP
(`codex mcp`); plugins + marketplaces (`codex plugin`); Starlark **execpolicy `.rules`** files
(user *and project* level); hooks with a trust model; `--ephemeral`; `--ignore-user-config`;
`-c key=value` overrides.

---

## 1. Primer — how agents actually work

*You asked how agents work "principally" and how to add them. This section is the answer.
Everything below is **files in your repo or home directory**. The VS Code extension and the
terminal CLI read the exact same files — choosing the extension changes nothing about the design.*

### 1.1 The seven primitives

| # | Primitive | What it is | Where it lives | When it loads |
|---|---|---|---|---|
| 1 | **Instruction file** | Always-on project context | `CLAUDE.md`, `AGENTS.md` (+ nested per-folder) | Every turn, always in context |
| 2 | **Subagent** | A *separate* agent with its own context window, tool allowlist and model | `.claude/agents/<name>.md` | Invoked by name, or auto-routed by its `description` |
| 3 | **Skill** | A procedure loaded *on demand* | `.claude/skills/<name>/SKILL.md` | Name + description always visible; body loads only when relevant |
| 4 | **Slash command** | A parameterized prompt you trigger | `.claude/commands/<name>.md` | On `/name` |
| 5 | **Hook** | Deterministic code at a lifecycle event — **can block the agent** | `settings.json` → `hooks` | On the event, always, with no model discretion |
| 6 | **MCP server** | External tools and data the agent can call | `.mcp.json` / `codex mcp` | Tools listed at session start |
| 7 | **Plugin** | A versioned bundle of 1–6, installable from a marketplace | `.claude-plugin/plugin.json` | `claude plugin install` |

### 1.2 The two mental models that matter

**(a) The model is rented; the repo is the system.**
A session's context window is scratch space that *will* be lost — to compaction, to a crash,
to closing VS Code. Anything that must survive belongs in git, the task DB, tests, architecture
docs, or a checkpoint file. v1 got this right and v2 keeps it verbatim.

**(b) Prompting is advisory; hooks are mandatory.**
This is the single biggest correction to v1. v1 encoded ownership rules as prose in `RULES.md`
and hoped agents would obey. They will not, reliably — under pressure a model edits the file it
needs. A `PreToolUse` hook returning exit code 2 makes the edit *impossible* and hands the agent
a reason to re-plan. **Every rule in v1 §14 that actually matters is re-expressed in v2 as a hook
or a permission rule.**

### 1.3 Anatomy of a subagent (the thing you will add most often)

`.claude/agents/test-author.md`:

```markdown
---
name: test-author
description: Writes and repairs tests. Use after any behavior change, or when a task is classified TEST_ONLY.
tools: Read, Grep, Glob, Edit, Write, Bash
model: sonnet
---

You write tests only. You never modify production source.
Read the task brief from the AgentKit MCP server before starting.
Run the project's test command from `.ai/project.yaml`.
Stop when tests pass, or when you can prove the production code is wrong —
in that case report it, do not fix it.
```

That is the whole thing. Frontmatter is wiring; the body is the system prompt. The `tools:` line
is real isolation — this agent cannot call a tool you leave out of that list.

### 1.4 The Codex equivalents

| Claude Code | Codex | Notes |
|---|---|---|
| `CLAUDE.md` | `AGENTS.md` | Codex reads `AGENTS.md` natively. **Make `CLAUDE.md` a one-line import of `AGENTS.md`** so there is one source of truth |
| Subagent file | **Config profile** `$CODEX_HOME/<role>.config.toml`, selected with `-p <role>` | Sets model, sandbox and approval policy per role |
| `.claude/skills/` | Prompts / plugin-provided instructions | Weaker. Keep procedures in `AGENTS.md` + skills and point Codex at them |
| Hooks (33 events) | Hooks (trust-gated) + **`.rules` execpolicy** | Codex's deterministic gate is the Starlark `.rules` file plus the sandbox |
| `.mcp.json` | `codex mcp` / `mcp_servers` in `config.toml` | **The same MCP server serves both.** This is the shared spine of the design |
| Plugins + marketplace | Plugins + marketplace | Both exist; ship two thin manifests over one payload |
| `--worktree` | `--worktree` / `--cd <dir>` | Same isolation model |

### 1.5 How to add an agent — in VS Code, no terminal

1. Create `.claude/agents/<name>.md` with frontmatter as above.
2. Save. It is live in the next session — no install step, no VS Code restart.
3. In the Claude panel type `@<name>`, or just describe the work; the `description:` field is
   what routes work to it automatically.
4. For Codex: create `$CODEX_HOME/<name>.config.toml`, then pick that profile in the ChatGPT
   panel (or pass `-p <name>`).

**Parallelism in VS Code with no orchestrator at all:** Claude Code's *agent view*
(`claude agents`, `/background`, and the agents panel in the extension) runs multiple sessions
concurrently, each able to take `--worktree`. Codex has an equivalent `agents` browser over its
local app-server daemon. **Phase 1 of this plan is fully usable through those two panels.**
The Python orchestrator in §7 adds scheduling, leases and gates on top — it is Phase 3, not a
prerequisite.

---

## 2. What changed from v1, and why

| # | v1 approach | v2 approach | Why |
|---|---|---|---|
| 1 | Manual `git worktree add` helpers | `claude --worktree` / `codex --worktree`, plus `worktree.symlinkDirectories` for `node_modules` and `sparsePaths` for large repos | Built in — and the symlink/sparse options solve the disk-bloat problem v1 never addressed |
| 2 | Ownership rules as prose in `RULES.md` | `PreToolUse` **MCP hook** → `lease_check(path)` → hard block | Prose is advisory; hooks are enforcement. **The core upgrade** |
| 3 | Agents write `handoff.json` by hand | `PreCompact` + `Stop` + `SubagentStop` hooks write the checkpoint automatically | v1's weakest link: it required a dying agent to remember to save. Now the runtime does it |
| 4 | "Sessions are disposable, rebuild from scratch" | Tiered: `--resume` if alive → `--fork-session` to branch → rebuild from checkpoint only if truly dead | Rebuilding from scratch every time burns tokens; resume and fork now exist |
| 5 | Continuation prompt pasted into a new session | `SessionStart` MCP hook injects brief + checkpoint automatically | Removes the human copy-paste step entirely |
| 6 | Parse agent prose to decide pass/fail | `--json-schema` (Claude) / `--output-schema` (Codex) → typed result objects | Review and merge gates become machine-readable instead of regex-on-English |
| 7 | Poll SQLite for heartbeats | Consume `stream-json` / `exec --json` events, with `--include-hook-events` | A real liveness signal instead of an inferred one |
| 8 | Roles described in markdown prose | Roles = `.claude/agents/*.md` + Codex profiles, each with its own model and tool allowlist | Roles become *enforced capability boundaries*, not job descriptions |
| 9 | Agents read/write `tasks.db` directly | Agents call an **MCP server**; only the orchestrator touches SQLite | Prevents state corruption and gives Claude and Codex identical semantics |
| 10 | Copy files into every repo | One **plugin** installed from a local marketplace; repos carry only `.ai/` state | Fix once, all projects inherit. `plugin tag` gives versioned releases |
| 11 | No cost control | `--max-budget-usd` per worker, per-role model tiering, `--effort` per role | Uncapped parallel agents is a runaway-spend risk v1 ignored |
| 12 | "Refactor hotspots first" (no method) | A measurable hotspot score, a `/decouple` skill, and a seam catalogue | v1 stated the requirement without a procedure. §10 is the procedure |
| 13 | Test gate = "run pytest" | Gates declared per project in `.ai/project.yaml`, executed by an **agentic verifier hook** | Works across Python / TS / UE C++ without rewriting the framework |
| 14 | Nothing about context economics | `@`-imports, skills instead of always-on prose, `promptCacheTtl`, `autoCompactWindow`, `/context` budget | Context is the scarce resource; v1 spent it on permanently-loaded documents |

**What v1 got right and v2 keeps unchanged:** permanent-vs-temporary memory; one worktree per
coding worker; `SAFE_PARALLEL` / `DEPENDENT` / `HOTSPOT` classification; one owner per hotspot;
contracts before parallelism; no worker merges to main; full tests after integration, not only
per branch; "maximum useful concurrency, not maximum agents".

---

## 3. Architecture

```
                        YOU  (VS Code: Claude panel + ChatGPT/Codex panel)
                         |
              +----------+-----------+
              |  /plan  slash command |   Architect subagent (opus, read-only tools)
              +----------+-----------+
                         |  writes task graph via MCP
                         v
        +==============================================+
        |          AgentKit MCP Server (Python)         |   <-- the spine
        |  tasks | leases | checkpoints | gates | briefs|
        +==============================================+
             ^              ^                    ^
             | MCP          | MCP                | MCP
     +-------+----+   +-----+------+      +------+------+
     | Claude     |   | Codex      |      | Orchestrator |
     | worker     |   | worker     |      | (scheduler)  |
     | worktree A |   | worktree B |      | supervisor   |
     +------------+   +------------+      +-------------+
           |                |                     |
           |  PreToolUse hook -> lease_check       | launches workers
           |  PreCompact hook -> checkpoint        | consumes JSONL events
           |  Stop hook -> verify gate             | enforces dependency graph
           v                v
        +--------------------------+
        |  integration branch      |
        |  review + full test gate |  <-- YOUR approval required (chosen autonomy level)
        +------------+-------------+
                     v
                    main
```

Three properties fall out of this shape:

1. **One source of truth for state.** Neither CLI owns the task graph — the MCP server does. Both
   read and write it through identical tool calls, so a task can be started by Claude and finished
   by Codex without translation.
2. **Enforcement sits between the model and the filesystem.** A worker physically cannot edit
   outside its lease, because the hook runs before the tool does.
3. **The orchestrator is optional.** Remove it and you still have working, safe, single-worker
   sessions driven from the VS Code panels. It adds throughput, not correctness.

---

## 4. The AgentKit repo layout

```
Multi_Agents_software_dev/                 <- this repo; the framework itself
  .claude-plugin/
    plugin.json                            # Claude plugin manifest
    marketplace.json                       # local marketplace so `plugin install` works offline
  plugin/
    agents/                                # the roles (§8), one .md each
      architect.md  lead-implementer.md  implementer.md
      test-author.md  reviewer.md  integrator.md  decoupler.md
    skills/
      plan-feature/SKILL.md                # feature package -> task graph
      decouple/SKILL.md                    # hotspot -> seams (§10)
      checkpoint/SKILL.md                  # handoff format + when to write
      migrate-project/SKILL.md             # onboard an existing repo (§6.3)
      verify/SKILL.md                      # run this project's declared gates
    commands/
      plan.md  work.md  status.md  handoff.md  integrate.md  onboard.md
    hooks/
      hooks.json                           # the enforcement layer (§6)
      scripts/                             # exec-form scripts, no shell quoting
    templates/
      AGENTS.md.tmpl  CLAUDE.md.tmpl
      project.yaml.tmpl  ownership.yaml.tmpl
      codex-profiles/*.config.toml
      codex.rules.tmpl
  orchestrator/                            # Python package (uv)
    agentkit/
      mcp_server.py                        # the spine — MCP stdio server
      db.py  models.py                     # SQLite schema + typed records
      scheduler.py                         # dependency + conflict aware
      supervisor.py                        # liveness from JSONL streams
      launcher_claude.py  launcher_codex.py
      gates.py                             # runs .ai/project.yaml commands
      hotspots.py                          # the churn x size x fan-in report
      cli.py                               # `agentkit` entrypoint
    pyproject.toml
    tests/
  codex-plugin/
    plugin.toml                            # Codex-side manifest over the same payload
  docs/
    00-how-agents-work.md                  # = §1, extracted for onboarding
    01-operating-model.md
    02-migration-playbook.md
  PLAN.md                                  # this file
  multi_agent_codex_claude_plan.md         # v1, retained
```

**Distribution.** `claude plugin marketplace add /path/to/AgentKit`, then
`claude plugin install agentkit@agentkit-local` — or pin it per-repo through
`extraKnownMarketplaces` + `enabledPlugins` in the project's `.claude/settings.json`, so cloning
the repo is enough to get the framework. Cut releases with `claude plugin tag`. Codex gets the
same payload via `codex plugin marketplace add`.

---

## 5. The per-project footprint

Deliberately tiny — behavior lives in the plugin, only *facts and state* live in the repo:

```
<your-project>/
  AGENTS.md                    # THE instruction file (Codex reads natively)
  CLAUDE.md                    # one line: @AGENTS.md
  .mcp.json                    # points at the AgentKit MCP server
  .claude/
    settings.json              # enabledPlugins + permissions.deny + worktree config (committed)
    settings.local.json        # personal, gitignored
  .codex/
    project.rules              # Starlark execpolicy for this repo
  .ai/
    project.yaml               # stack, gate commands, hot paths, budgets
    ownership.yaml             # module -> owner + paths
    architecture.md            # module boundaries, contracts, data flow
    tasks.db                   # SQLite (gitignored; rebuildable from tasks.yaml)
    tasks.yaml                 # human-readable task graph (committed)
    runtime/task-<id>/
      handoff.json             # auto-written by hooks
      notes.md
```

### 5.1 `.ai/project.yaml` — the file that makes the framework stack-agnostic

```yaml
name: example_app
stacks: [python, react]

gates:
  fast:   ["python -m pytest -q -x -m 'not slow'", "python -m ruff check ."]
  full:   ["python -m pytest -q", "npm --prefix apps/web run test"]
  types:  ["python -m mypy services routes"]
  build:  ["npm --prefix apps/web run build"]

hot_paths:                      # never editable without an explicit HOTSPOT lease
  - routes/video.py
  - services/providers.py
  - apps/web/src/components/editor/Editor.jsx

contracts:                      # changing these requires an architect task
  - contracts/**
  - packages/api-contracts/**
  - alembic/versions/**

budgets:
  worker_usd: 3.00
  integration_usd: 5.00

models:
  architect: opus
  lead: opus
  implementer: sonnet
  test_author: sonnet
  reviewer: sonnet
  mechanical: haiku
```

Every skill, hook and gate in the framework reads this file. Onboarding a new project is
essentially *writing this file*, which is why §6.3 can be largely automated.

---

## 6. The enforcement layer

The settings schema exposes **five hook types**, and the choice between them is the main design
lever:

| Type | Use it for | Cost |
|---|---|---|
| `command` (with `args` = exec form, no shell) | Formatters, git checks, checkpoint writers | ~0 |
| `mcp` (calls an MCP tool directly, with `${tool_input.file_path}` interpolation) | **Lease checks, brief injection, checkpoint writes** | ~0 |
| `prompt` (small model judges, can block) | "Does this diff exceed the task scope?" | cents |
| `agent` (agentic verifier) | "Verify the declared tests actually ran and passed" | cents |
| `http` | Dashboards, notifications, remote state | ~0 |

The `mcp` hook type is the key discovery: **ownership enforcement is a direct MCP call with no
shell script**, which sidesteps Windows path-quoting entirely.

### 6.1 `plugin/hooks/hooks.json` (design)

| Event | Matcher | Type | Effect |
|---|---|---|---|
| `SessionStart` | — | `mcp` → `agentkit.brief` | Injects task brief + last checkpoint. Replaces v1's pasted continuation prompt |
| `PreToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit` | `mcp` → `agentkit.lease_check` | **Blocks writes outside the task's lease**; returns the owning task id so the agent can request a graph change |
| `PreToolUse` | `Bash` `if: Bash(git push *)` | `command` | Blocks pushes to `main` / integration from a worker |
| `PreToolUse` | `Bash` `if: Bash(git merge *)` | `command` | Only the integrator role may merge |
| `PostToolUse` | `Edit\|Write` | `command` (async) | Formats + lints only the changed file; fast feedback |
| `PreCompact` | — | `mcp` → `agentkit.checkpoint` | **Auto-checkpoint before context is compacted.** Fixes v1's biggest failure mode |
| `Stop` | — | `agent` verifier | "Verify the gate commands for this task ran and passed." Blocks a premature finish |
| `Stop` / `SubagentStop` | — | `mcp` → `agentkit.checkpoint` + status → `REVIEW` | Hands the task back to the graph |
| `SubagentStart` | — | `mcp` → `agentkit.heartbeat` | Liveness |
| `WorktreeCreate` | — | `command` | Registers the worktree, seeds `.ai/runtime/task-<id>/` |
| `PermissionDenied` | — | `http`/`command` | Logs every out-of-scope attempt — the best signal that the task graph is wrong |
| `TaskCreated` / `TaskCompleted` | — | `mcp` | Orchestrator telemetry without polling |

Use `async: true` for formatters, and `asyncRewake: true` for long test runs so the agent is woken
when they fail instead of blocking on them.

### 6.2 Permissions (defence in depth, in `.claude/settings.json`)

```jsonc
{
  "permissions": {
    "deny": [
      "Edit(.ai/tasks.db)",           // state is MCP-only
      "Edit(.env)", "Read(.env)",
      "Edit(alembic/versions/**)",    // migrations need an architect task
      "Bash(git push origin main*)"
    ],
    "ask": ["Bash(git merge *)", "Bash(docker *)"],
    "defaultMode": "acceptEdits"      // workers run hands-off inside their worktree
  },
  "worktree": {
    "symlinkDirectories": ["node_modules", ".venv", ".cache"],
    "baseRef": "head"
  }
}
```

### 6.3 The Codex side

Codex has no equivalent of the 33-event hook surface, so its enforcement is layered differently
and just as effective:

1. **The worktree is the boundary** — `codex exec --cd <worktree> -s workspace-write`. Codex
   cannot write outside the workspace it was given.
2. **`.codex/project.rules`** (Starlark execpolicy) gates *commands*, in the same shape as the
   `prefix_rule(pattern=[...], decision="allow")` entries already in your `~/.codex/rules/default.rules`.
3. **The same MCP server** supplies briefs, leases and checkpoints — so a Codex worker asks for its
   lease explicitly, and the orchestrator verifies the resulting diff against the lease at the merge
   gate. Enforcement moves from *pre-write* to *pre-merge*, which is the honest trade.
4. **Per-role profiles** (`$CODEX_HOME/<role>.config.toml`) pin model, sandbox and approval policy.

> **Consequence for assignment:** give Codex `SAFE_PARALLEL` and test/review work, and give Claude
> `HOTSPOT` work where pre-write blocking matters most. This is a capability-driven split, not a
> preference — and it should be revisited as Codex's hook system stabilises out of alpha.

---

## 7. The orchestrator (Python + uv)

A single package exposing two entry points: an **MCP server** (what agents talk to) and a **CLI**
(what you and the scheduler use).

```bash
uv tool install --editable ./orchestrator
agentkit init            # writes .ai/, AGENTS.md, .mcp.json into the current repo
agentkit plan "add retry, caching, cost tracking"   # architect -> task graph
agentkit run --max-workers 3                        # scheduler + supervisor loop
agentkit status                                     # task graph + leases + spend
agentkit hotspots                                   # the decoupling report (§10)
```

### 7.1 MCP tools exposed to agents

| Tool | Purpose |
|---|---|
| `brief(task_id)` | Everything a session needs to start or resume: goal, owned paths, contracts, gates, last checkpoint, `next_action` |
| `lease_check(path, task_id)` | The enforcement primitive called by the `PreToolUse` hook |
| `lease_request(paths, reason)` | A worker asking to widen scope — creates a review item instead of silently editing |
| `checkpoint(task_id, payload)` | Writes `handoff.json`; called by hooks, not by the model's goodwill |
| `task_status(task_id, status, evidence)` | State transitions, with evidence required for `REVIEW` |
| `conflict_check(paths)` | Pre-flight overlap test against all active leases |
| `gate_run(task_id, level)` | Runs `fast` / `full` / `types` from `.ai/project.yaml`, returns typed results |
| `graph_amend(proposal)` | The escape hatch from v1 §14.10 — a worker that needs unowned territory proposes a graph change |

### 7.2 Launching workers

```python
# Claude worker — enforcement pre-write, structured result, capped spend
claude -p "@agentkit brief for task 101, then execute it."
  --worktree wt-101 --model sonnet --effort high
  --permission-mode acceptEdits
  --session-id <uuid> --output-format stream-json --include-hook-events
  --json-schema <result-schema> --max-budget-usd 3.00
  --settings .agentkit/worker-settings.json

# Codex worker — worktree as the boundary, typed final message
codex exec --cd ../wt-102 -s workspace-write -p implementer
  --json --output-schema result.schema.json -o last.json
  "Read the AgentKit brief for task 102 and execute it."
```

Recovery is tiered, replacing v1's blunt "always rebuild":

| Situation | Action |
|---|---|
| Session alive, context filling | `PreCompact` hook already checkpointed; let it compact and continue |
| Session alive, wrong direction | `--fork-session` from a good point |
| Process dead, worktree intact | `--resume <session-id>` in place |
| Session unrecoverable | New session seeded by `SessionStart` → `brief()` → continues from `next_action` |
| Repeated gate failures (n≥3) | Escalate to architect; the task is probably mis-scoped, not badly implemented |

### 7.3 Supervision

Consume the JSONL event stream rather than polling. A worker is *stalled* when: no event for
N minutes, **or** no commit for M minutes with a dirty tree, **or** three consecutive gate
failures, **or** it has hit `--max-budget-usd`. Each has a distinct recovery path.

---

## 8. Roles (as enforced capability boundaries)

| Role | Impl | Model | Tools | Writes to |
|---|---|---|---|---|
| **Architect** | `agents/architect.md` | opus | Read, Grep, Glob, MCP | `.ai/tasks.yaml`, `.ai/architecture.md` — **no source code** |
| **Decoupler** | `agents/decoupler.md` | opus | Read, Edit, Bash, MCP | Hotspot files only, under an exclusive lease (§10) |
| **Lead implementer** | `agents/lead-implementer.md` | opus | full | The current HOTSPOT, one at a time |
| **Implementer** | `agents/implementer.md` | sonnet | full minus merge | One module, `SAFE_PARALLEL` only |
| **Test author** | `agents/test-author.md` | sonnet | no Write to source | `tests/**` only |
| **Reviewer** | `agents/reviewer.md` | sonnet | read-only + MCP | Nothing — emits a typed verdict |
| **Integrator** | `agents/integrator.md` | sonnet | Bash(git *) | The only role permitted to merge |

The "writes to" column is not documentation — it is the lease the hook enforces, and for Claude
roles the `tools:` frontmatter enforces the rest.

Claude and Codex are both eligible for any role (v1's principle, kept). The only asymmetry is the
capability-driven one in §6.3.

---

## 9. Git and merge strategy

Unchanged from v1 in shape, with the mechanics updated:

```
main
  └── feature/<package>                 integration branch
        ├── agent/task-101-generator-abstraction    (HOTSPOT, exclusive)
        ├── agent/task-102-provider-veo             (SAFE_PARALLEL)
        └── agent/task-103-retry-manager            (SAFE_PARALLEL)
```

- Worktrees come from `--worktree` with `baseRef: head` and `symlinkDirectories` for `node_modules`
  and `.venv` — otherwise three worktrees of a large application repo is many GB of duplication.
- Workers never merge. The integrator rebases onto the integration branch, runs the `full` gate,
  and merges.
- **Your chosen autonomy level:** workers run unattended inside their worktrees; **integration →
  main requires your approval**. A failed gate bounces the task back to its worker with the typed
  failure attached, rather than to you.

---

## 10. Decoupling: refactoring for parallelism

v1 said "refactor hotspots first" without saying how. This is the how, and it is the part that
matters most for your codebase.

### 10.1 Measure before refactoring

`agentkit hotspots` scores every file:

```
hotspot_score = commit_frequency(90d) × log(lines) × fan_in
```

- **commit frequency** — from `git log --format= --name-only --since=90.days | sort | uniq -c`
- **fan-in** — how many modules import or route through it
- **lines** — size proxy for merge-conflict surface

A file with high churn *and* high size *and* high fan-in is a file that will serialise your agents
no matter how you schedule them. That is the refactor queue, in order.

### 10.2 The seam catalogue

Apply in this order — earlier seams are cheaper and unblock more:

| Seam | Cuts along | Gives each agent |
|---|---|---|
| **Router split** | HTTP endpoint groups | One router file per domain |
| **Provider/strategy** | Interchangeable backends | One file per provider |
| **Pipeline stages** | Sequential phases | One stage module + its tests |
| **Contract extraction** | Frontend ↔ backend | A generated schema both sides code against, in parallel |
| **Hook/component split** | UI logic vs rendering | One hook or subcomponent per feature |
| **Config/registry** | "add a case" changes | A data entry instead of a code edit |

The registry seam is the highest-leverage: once adding a provider means adding a file plus one
registry line, N agents can add N providers with a one-line conflict surface each.

### 10.3 The decoupling protocol

1. Architect produces a **seam plan** with target modules and a public interface, and writes it to
   `.ai/architecture.md`. No implementation yet.
2. **Characterisation tests first.** The test author writes tests against *current* behavior,
   through the interface that will survive. These are the safety net; they must pass before and
   after, unchanged.
3. A single **decoupler** takes an exclusive lease on the hotspot and performs a *pure move* —
   no behavior changes, no new features, in one reviewable commit series.
4. The gate for a decoupling task is unusually strict: **characterisation tests must pass with zero
   edits to the test files.** Any test edit means behavior changed, and the task fails.
5. Only then is the hotspot lease released and the fan-out tasks become `READY`.

**Rule: never mix a decoupling and a feature in one task.** It destroys reviewability and is the
most common way an agent silently changes behavior.

---

## 11. The pilot

The original plan ended with a measured pilot on a private application repository:
its hotspot table, proposed module splits and step-by-step sequence. Those
project-specific details are omitted from this public copy. The method is §10's
decoupling protocol: measure hotspots, add characterisation tests, split one hotspot
under an exclusive lease with no behavior change, then run two agents in parallel on
the new modules and confirm the hook blocks a deliberate collision.

---

## 12. Build phases

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0. Tooling** | `codex.exe` on PATH; marketplace registered; `uv tool install` works | `claude plugin list` and `codex plugin list` both show agentkit |
| **1. Static framework (no orchestrator)** | Plugin with 7 agents, 5 skills, 6 commands, `AGENTS.md`/`project.yaml` templates | A single VS Code session can `/plan`, `/work`, `/handoff` in a pilot repo |
| **2. Enforcement** | MCP server with `brief`/`lease_check`/`checkpoint`; hooks wired | An agent is *provably blocked* editing an unowned path, and auto-checkpoints on compact |
| **3. Scheduler** | SQLite graph, `READY`/`BLOCKED`, `agentkit run --max-workers N` | Three workers run in three worktrees with zero collisions |
| **4. Gates** | `gate_run`, typed review verdicts, integrator role, merge queue | A failed gate bounces to the worker, not to you |
| **5. Decoupling product** | `agentkit hotspots`, `/decouple` skill, characterisation-test gate | The pilot's first two hotspot splits complete with unchanged tests |
| **6. Second repo** | Onboard a second repo on a different stack | `agentkit init` + gate config is the entire cost of adoption |

Phases 1–2 deliver most of the value. **Do not build Phase 3 until Phase 2 has caught a real
collision** — if nothing is being blocked, the scheduler has nothing to schedule safely.

---

## 13. Risks and honest limits

| Risk | Mitigation |
|---|---|
| Codex is **0.154.0-alpha**; hooks and plugin APIs may shift | Keep Codex integration behind `launcher_codex.py`; the MCP spine is vendor-neutral, so a Codex change never touches the core |
| Codex lacks pre-write hooks | Worktree + sandbox + pre-merge lease audit (§6.3). Assign hotspot work to Claude until this changes |
| Plugin hooks are machine-global | Scope every hook with `if:` filters and have `lease_check` no-op when `.ai/` is absent, so non-AgentKit repos are unaffected |
| Worktree disk cost on a repo with `node_modules`, `.venv`, model caches | `symlinkDirectories` + `sparsePaths`; cap `--max-workers` by free disk, not CPU |
| Agents auto-committing garbage | Gate on `fast` before commit; `full` before merge; `/rewind` file checkpointing stays on |
| Runaway spend across parallel workers | `--max-budget-usd` per worker, per-role model tiering, `agentkit status` shows live spend |
| The framework becomes a project of its own | Phase exit criteria above; every phase must be usable on the pilot repo the day it ships |
| Six instruction files drifting (e.g. `AGENTS.md` + `CLAUDE.local.md` + `GEMINI.md` side by side) | `AGENTS.md` is canonical; every other file is a one-line import. `agentkit init` enforces this |

---

## 14. Final principle (from v1, unchanged and still correct)

Treat Codex and Claude as **replaceable compute** around a permanent engineering system.

**Permanent:** repository, git history, tests, architecture docs, task graph, leases, checkpoints,
integration state.
**Temporary:** individual sessions, their context windows, their reasoning history.

v2 adds one clause: *make the permanent system enforce itself.* A rule a model can ignore is not
a rule — it is a suggestion, and suggestions do not survive contact with a context window that is
95% full.
