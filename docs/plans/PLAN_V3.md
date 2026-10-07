> Historical design: use [current documentation](../README.md) and the installed
> source for present behavior. This plan is reference material, not a worker brief.

# AgentKit — Plan v3
### Production-grade design for maximum safe concurrency on an existing codebase

> Supersedes `PLAN.md` (v2), which supersedes `multi_agent_codex_claude_plan.md` (v1).
> v3 changes no architecture that was working. It removes overstated guarantees,
> replaces vendor assumptions with measured capabilities, resolves two places where
> state had no single owner, and adds the recovery and audit machinery that the
> difference between a demo and production actually consists of.
>
> New to the vocabulary? [GLOSSARY.md](../GLOSSARY.md) explains every term in plain English.

---

## v3 design goals

1. **Every guarantee is technically accurate.** Where the system prevents something, say
   prevented. Where it only detects, say detected. Never claim impossible for something
   merely inconvenient.
2. **Capability-based, not vendor-based.** No rule anywhere says "Claude does X, Codex does
   Y". Agents declare or are probed for capabilities; the scheduler assigns on that basis.
3. **Exactly one authority per kind of state.** No fact is owned by two systems.
4. **Restart-safe by construction.** Killing the orchestrator, a worker, or the machine at
   any instant leaves recoverable state and never duplicates work.
5. **Deterministic recovery, classified by failure.** No generic "restart the agent".
6. **The core does not import a vendor.** Vendor specifics live in adapters only.
7. **Justified complexity.** Every component names the failure it prevents, or is deleted.
8. **Self-proving.** AgentKit ships an acceptance suite that demonstrates its own
   guarantees before anyone trusts it with a real repository.

Target, restated: **maximum safe useful concurrency on an existing codebase, with
deterministic recovery and minimal human babysitting.** Not "more agents".

---

## 0. Capability verification

Every external claim below was checked against the versions installed on this machine on
2026-09-13. Claims are graded:

| Grade | Meaning |
|---|---|
| **STABLE** | Documented surface, verified present, safe to depend on |
| **EXPERIMENTAL** | Present but flagged experimental or off by default |
| **VERSION-SPECIFIC** | Verified on this build; may differ on another |
| **UNVERIFIED** | Believed true, not confirmed — must not be a core dependency |

### 0.1 Claude Code — 2.1.167 CLI / 2.1.270 VS Code extension

| Capability | Grade | Evidence |
|---|---|---|
| `--worktree`, `worktree.symlinkDirectories` / `sparsePaths` / `baseRef` | STABLE | `--help`, settings schema |
| Hooks: 33 events × 5 types (`command`, `prompt`, `agent`, `http`, `mcp`) | VERSION-SPECIFIC | settings schema enum |
| `PreToolUse` blocking via exit code 2 | STABLE | verified live (§17.2 collision test passes) |
| `--session-id` / `--resume` / `--fork-session` | STABLE | `--help` |
| `--output-format stream-json`, `--include-hook-events` | STABLE | `--help` |
| `--json-schema` structured output | VERSION-SPECIFIC | `--help` |
| `--max-budget-usd` | VERSION-SPECIFIC | `--help`, print mode only |
| Plugins + marketplaces + `plugin tag` | STABLE | `claude plugin --help`, live install |
| OS sandbox with `network.allowedDomains` / `strictAllowlist` | VERSION-SPECIFIC | settings schema |
| `permissions.deny` on `Bash(...)` patterns | STABLE | settings schema |
| Background agents (`claude agents`, `/background`) | VERSION-SPECIFIC | `claude agents --help` |

### 0.2 Codex — 0.154.0-alpha.6.1 (bundled in the ChatGPT VS Code extension)

| Capability | Grade | Evidence |
|---|---|---|
| `exec --json`, `exec resume`, `exec fork` | VERSION-SPECIFIC | `--help`; alpha build |
| `--cd`, `--add-dir`, `-s {read-only,workspace-write,danger-full-access}` | VERSION-SPECIFIC | `--help` |
| `--output-schema`, `-o/--output-last-message` | VERSION-SPECIFIC | `--help` |
| Profiles (`-p` layering `$CODEX_HOME/<name>.config.toml`) | VERSION-SPECIFIC | `--help` |
| **Hooks — feature flag `hooks` = stable/enabled** | VERSION-SPECIFIC | `codex features list` |
| **Hook events**: `PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, SessionStart, SessionEnd, UserPromptSubmit, SubagentStart, SubagentStop, Stop, Interrupt` | UNVERIFIED (schema) | enum recovered from binary |
| **`PreToolUseDecision`** wire values `approve/block/allow/deny`; payload carries `tool_name`, `tool_input`, `permission_mode` | UNVERIFIED (schema) | binary strings; config via `hooks.json`/`hooks.toml` |
| **`worktrees` — `experimental`, default `false`** | EXPERIMENTAL | `codex features list` |
| Starlark execpolicy `.rules`, user *and* project level | VERSION-SPECIFIC | `--ignore-rules` help text; live file |
| MCP (`codex mcp`, `mcp_servers` in config) | VERSION-SPECIFIC | `--help` |
| `codex doctor` machine-readable health | VERSION-SPECIFIC | live run |

### 0.3 Two v2 claims that were wrong

**v2 said Codex has no pre-write hook**, and built an asymmetry on it — hotspots to Claude,
safe-parallel to Codex. The binary exposes `PreToolUse` with a block decision and the
feature flag reports `hooks stable true`. **The asymmetry is deleted.** This is precisely
the failure mode §3's capability model exists to prevent: a vendor assumption, hardcoded,
silently wrong.

**v2 listed Codex `--worktree` as available.** The flag parses, but `worktrees` is
`experimental` and **off by default**. The Codex adapter therefore declares
`worktree_native: false` until the flag is enabled, and AgentKit creates the worktree
itself with `git worktree add`, passing `--cd`. Core is unaffected — this is an adapter
detail, which is the point of §1.

### 0.4 Standing rule

No STABLE-graded claim may be load-bearing without a probe that re-verifies it at runtime
(§3.3). No EXPERIMENTAL or UNVERIFIED claim may be a core dependency at all — it may only
be an *optimisation* an adapter offers, with a defined fallback.

---

## 1. Core and adapters

The single most important structural change in v3.

```
┌─────────────────────────────── CORE AGENTKIT ───────────────────────────────┐
│  task graph · state machine · leases · overlap prediction · scheduler        │
│  checkpoints · gates · git integration · recovery · observability · audit    │
│                                                                              │
│  Depends on: git, SQLite, a filesystem, POSIX-ish process control.           │
│  Does NOT import, reference, or branch on any vendor.                        │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │  AgentAdapter interface
       ┌───────────────┬───────────┼───────────┬───────────────┬──────────────┐
   ClaudeCode       Codex      OpenCode      Aider        Gemini/Jules     future
```

### 1.1 The adapter interface

An adapter is the *only* place a vendor name may appear:

```python
class AgentAdapter(Protocol):
    name: str

    def detect(self) -> Installation | None:
        """Locate the binary and read its version. None if absent."""

    def probe(self, sandbox_dir: Path) -> CapabilitySet:
        """Measure what this installation can actually do. See §3.3."""

    def build_launch(self, task: Task, worktree: Path, role: Role) -> Launch:
        """argv + env + cwd. Core never constructs a command line."""

    def install_guards(self, worktree: Path, task: Task) -> GuardReport:
        """Write whatever hook/permission/policy files this agent honours.
        Returns which enforcement layers are actually active — core uses this
        to decide whether the task may run unattended."""

    def parse_events(self, stream: IO[str]) -> Iterator[AgentEvent]:
        """Normalise the vendor's output into core's event vocabulary."""

    def resume(self, task: Task) -> Launch | None:
        """None if this agent cannot resume; core then replays from checkpoint."""
```

Core calls only these. If `build_launch` disappeared tomorrow for one vendor, no core file
changes.

### 1.2 What this buys

The `GuardReport` is the load-bearing part. It makes enforcement *measured* rather than
assumed: core asks "which layers are live for this worker?" and refuses to schedule a
HOTSPOT task onto an agent whose report shows no pre-write guard. That is §3's capability
model applied at the point of scheduling.

---

## 2. Enforcement: prevention, detection, merge-time

### 2.1 The honest problem statement

v2 claimed: *"A worker physically cannot edit outside its lease."* **That was false.** A
`PreToolUse` hook on `Edit|Write|MultiEdit` guards one channel. A worker mutates files
through many others:

| Channel | Example |
|---|---|
| Shell redirection | `echo x > ../other/file.py`, `cat a >> b` |
| In-place editors | `sed -i`, `perl -pi -e`, `patch`, `git apply` |
| File operations | `cp`, `mv`, `rm`, `install`, `rsync` |
| Interpreters | `python -c "open('x','w').write(...)"`, `node -e`, `bash script.sh` |
| Git plumbing | `git checkout <ref> -- path`, `git restore`, `git stash pop`, `git reset --hard` |
| Package managers | `npm install` rewriting `package-lock.json`, `uv sync` rewriting `uv.lock` |
| Formatters/codegen | `ruff format .`, `prettier --write .`, `openapi-generator`, `alembic revision` |
| Test side effects | snapshot updates, fixture regeneration, `.pytest_cache` |
| Build systems | artifacts, `dist/`, compiled assets |
| MCP servers | a filesystem MCP server writing directly, outside the tool-permission model |
| Symlinks | a link inside the worktree pointing out of it |
| Subagents | a spawned subagent that did not inherit the task environment |

Any design that ignores these is not an enforcement model; it is a speed bump on one road.

### 2.2 Seven layers, each honestly labelled

| # | Layer | Kind | Covers | Defeated by |
|---|---|---|---|---|
| L0 | **Worktree boundary** | PREVENTION | writes to other tasks' trees | absolute paths, `cd ..` |
| L1 | **OS/agent sandbox** (`workspace-write`, Claude sandbox) | PREVENTION | all writes outside the workspace, incl. shell | unsandboxed platforms; `dangerouslyDisableSandbox` |
| L2 | **Tool permissions** (`deny` rules) | PREVENTION | named dangerous commands | commands not on the list |
| L3 | **Pre-write file guard** (`PreToolUse`) | PREVENTION | `Edit/Write/MultiEdit/NotebookEdit` | any non-tool mutation |
| L4 | **Shell-command guard** (`PreToolUse` on Bash) | PREVENTION (partial) | statically-decidable write commands | opaque commands — deny-by-default, see 2.4 |
| L5 | **Worktree audit** (`git status` + mtime sweep) | DETECTION | *every* channel, inside the worktree | nothing, within the worktree |
| L6 | **Pre-commit lease validation** (git hook) | PREVENTION at commit | anything reaching a commit | `--no-verify` (denied at L2, re-caught at L7) |
| L7 | **Merge gate** (full diff vs lease) | ENFORCEMENT | everything that could reach integration | nothing — deterministic and final |

L5 is the layer v2 lacked entirely, and it is the one that makes the model complete
*inside* a worktree. L7 is the one that makes the guarantee absolute.

### 2.3 The guarantee, stated accurately

> **GUARANTEED (prevention, deterministic):** no change outside a task's lease reaches the
> integration branch or `main`. Enforced at L7 by comparing the complete changed-file set
> against the lease, with no model involvement.
>
> **GUARANTEED (prevention) for the tool channel:** `Edit`/`Write`/`MultiEdit`/
> `NotebookEdit` outside the lease are blocked before the write, on any agent whose
> `GuardReport` includes `prewrite_file_guard`.
>
> **GUARANTEED (prevention) for cross-worktree writes** on any agent whose `GuardReport`
> includes `workspace_sandbox`.
>
> **DETECTED, not prevented:** mutations through shell, interpreters, package managers,
> code generators and build systems *inside the worker's own worktree*. These are caught
> by L5 within one tool-batch, blocked at L6 before they can be committed, and blocked
> unconditionally at L7.
>
> **NOT COVERED:** anything a worker does outside the repository — network calls, global
> package installs, machine state. That is §16's problem, not the lease model's.

Every one of those sentences is implemented by a named layer and exercised by a named test
in §17.

### 2.4 The exact algorithms

**Path authorisation** — one function, called by L3, L4, L5, L6 and L7. Identical logic
everywhere is a requirement, not an optimisation: a plan that passes the scheduler's check
must never be blocked at edit time, and a worker that was allowed to edit must never be
rejected at merge.

```
authorize_write(path, task, project) -> Decision:
    rel := normalize_to_repo_relative(path)
    if rel is None or escapes_worktree(rel):        return DENY("escape")
    if matches(ALWAYS_PROTECTED, rel):              return DENY("protected_state")
    if task is None:                                return ALLOW("unmanaged_session")

    for claim in foreign_claims(task):              # other live tasks' leases + owned_paths
        if matches(claim.glob, rel):                return DENY("owned_by_other", claim.task)

    if matches(task.owned_paths, rel):              return ALLOW("owned")
    if matches(project.protected, rel):             return DENY("protected_path")
    if task.owned_paths is non-empty:               return DENY("out_of_scope")
    return ALLOW("unscoped_task")                   # task not yet planned: permissive
```

**Shell-command classification (L4).** Shell is not statically decidable in general, so the
policy is explicit about what it cannot know:

```
classify_command(cmd) -> Verdict:
    for simple_cmd in split_on(; && || | newline):
        prog, args, redirects := parse(simple_cmd)

        if redirects contain > or >>:        writes += targets(redirects)
        if prog in READONLY_ALLOWLIST and no redirects:   continue
        if prog in MUTATORS:                 writes += static_targets(prog, args) or UNKNOWN
        if prog in INTERPRETERS and (-c/-e or script arg):  writes += UNKNOWN
        if prog in PKG_MANAGERS:             writes += lockfile_paths(project) or UNKNOWN
        if prog not in any known set:        writes += UNKNOWN

    if UNKNOWN in writes:
        if cmd matches project.allowlisted_commands:  return ALLOW_AUDITED   # gate commands
        return DENY("opaque_write: cannot prove target is in scope")

    return ALLOW if all(authorize_write(t).allowed for t in writes) else DENY
```

`project.allowlisted_commands` is seeded automatically from `.ai/project.yaml`'s `gates:`
— the commands the project already declares it runs. `ALLOW_AUDITED` means: permitted, and
L5 runs immediately afterwards rather than on the usual schedule.

**Worktree audit (L5).** Complete within the worktree, independent of mutation channel:

```
audit_worktree(task, worktree) -> [Violation]:
    changed := git_status_porcelain(worktree)                  # untracked + modified + staged
             ∪ git_diff_name_only(worktree, task.base_sha)     # already committed
    changed -= generated_paths(project)                        # §11.4, regenerated not merged
    return [v for p in changed if not (v := authorize_write(p, task)).allowed]
```

Cost: one `git status` on a warm repo, single-digit milliseconds. Run on `PostToolUse`
(async), after every `ALLOW_AUDITED` command, on `Stop`, and before every commit.

**Pre-commit validation (L6).** Installed per worktree by `install_guards`, via
`git config core.hooksPath .ai/githooks` *scoped to that worktree*:

```
pre-commit:
    staged := git diff --cached --name-only
    violations := [p for p in staged if not authorize_write(p, task).allowed]
    if violations: print violations; exit 1
```

`Bash(git commit * --no-verify*)` and `-n` are denied at L2, and L7 re-checks regardless,
so bypassing L6 buys nothing.

**Merge gate (L7)** — the authority:

```
merge_gate(task, integration_branch) -> Verdict:
    # The commit the lease was granted against, not a merge-base computed now:
    # a merge-base can drift forward past the very changes being audited.
    base    := task.base_sha or git merge-base(integration_branch, task.branch)
    changed := git diff --name-only base..task.branch
    violations := [p for p in changed if not authorize_write(p, task).allowed]
    if violations:  return REJECT(violations)         # branch quarantined, never merged
    if not gate_run(task, "full").passed: return REJECT(gate_output)
    return ACCEPT
```

### 2.5 Where each layer is configured

| Layer | Claude Code adapter | Codex adapter | Core fallback if absent |
|---|---|---|---|
| L0 | `git worktree add`; `--worktree` when native | `git worktree add` + `--cd` (worktrees experimental) | core creates it |
| L1 | `sandbox.enabled`, `network.allowedDomains` | `-s workspace-write` | none — task marked `weak_isolation` |
| L2 | `permissions.deny` | `.codex/project.rules` (Starlark) | none |
| L3 | `PreToolUse` hook → `agentkit-hook` | `PreToolUse` hook (schema UNVERIFIED) | none — L5/L6/L7 carry it |
| L4 | `PreToolUse` matcher `Bash` | `PreToolUse` + execpolicy | none |
| L5 | core, on `PostToolUse`/`Stop` | core, on `PostToolUse`/`Stop` | core polls on a timer |
| L6 | core (git hook, vendor-independent) | same | — |
| L7 | core | core | — |

L5–L7 are pure core and work for **every** agent, including ones with no hook system at
all. That is what makes an Aider or OpenCode adapter viable without weakening the
guarantee that matters.

---

## 3. Capabilities, not vendors

### 3.1 The capability set

```yaml
# Measured per installation, cached in .ai/capabilities.json
capabilities:
  prewrite_file_guard:    true    # can block a file write before it happens
  shell_guard:            true    # can block a shell command before it runs
  workspace_sandbox:      true    # OS-enforced write confinement
  network_control:        true    # domain allow/deny
  resume_session:         true
  fork_session:           true
  structured_output:      true    # typed final result, not prose
  mcp_stdio:              true
  worktree_native:        false   # core creates the worktree instead
  budget_cap:             true
  event_stream:           true    # incremental events for supervision
  subagents:              true
```

### 3.2 Derived classes, and what each task type requires

Core never reasons about raw flags; it reasons about these:

```yaml
derived:
  strong_write_isolation: workspace_sandbox and (prewrite_file_guard or shell_guard)
  structured_checkpointing: mcp_stdio and structured_output
  recoverable: resume_session or (mcp_stdio and structured_checkpointing)
  supervisable: event_stream or budget_cap

requirements:
  HOTSPOT:         [strong_write_isolation, structured_checkpointing, recoverable]
  DECOUPLE:        [strong_write_isolation, structured_checkpointing, recoverable]
  CONTRACT_CHANGE: [strong_write_isolation, structured_checkpointing]
  SAFE_PARALLEL:   [recoverable]
  TEST_ONLY:       [recoverable]
  RESEARCH:        []              # read-only work needs nothing
```

An agent lacking `strong_write_isolation` is not banned — it is simply never assigned work
where a mistake is expensive, and may still run `SAFE_PARALLEL` and `TEST_ONLY` tasks under
L5–L7. Nothing anywhere names a vendor.

### 3.3 Probing, not assuming

`agentkit probe` runs at install, and re-runs automatically when an adapter's detected
version string changes. It does not read a table of known versions — it *measures*:

| Probe | Method | Proves |
|---|---|---|
| `prewrite_file_guard` | launch the agent in a throwaway repo with a guard that blocks one path; instruct it to edit that path; check the file is unchanged | the hook actually fires and blocks |
| `shell_guard` | same, via `echo x > guarded.txt` | Bash channel is covered |
| `workspace_sandbox` | instruct a write to a temp path outside the workspace | confinement is real |
| `resume_session` / `fork_session` | start a session, record the id, resume it, ask for a fact only the first session knew | resume genuinely restores context |
| `structured_output` | request a response against a fixed schema; validate | typed results are parseable |
| `mcp_stdio` | start with the AgentKit server; ask it to call `gate_list` | the spine is reachable |
| `event_stream` | count events on a trivial task | supervision is possible |
| `budget_cap` | flag present and accepted | spend is boundable |

A probe that fails downgrades the capability to `false` and writes the reason into
`capabilities.json`. **A capability is never assumed from a version number.** This is the
mechanism that would have caught both v2 errors in §0.3 automatically.

```json
{
  "adapter": "codex", "version": "0.154.0-alpha.6.1", "probed_at": "2026-09-13T12:00:00Z",
  "capabilities": { "prewrite_file_guard": true, "worktree_native": false },
  "notes": { "worktree_native": "feature flag 'worktrees' is experimental and disabled" }
}
```

---

## 4. State authority

### 4.1 One owner per fact

| State | Authority | Format | Committed? | Rebuildable from |
|---|---|---|---|---|
| **Desired task graph** (spec) | `.ai/tasks.yaml` | YAML | yes | authored by architect + human |
| **Runtime execution state** | SQLite `.ai/tasks.db` | SQLite | **no** | tasks.yaml + git + handoffs (§4.3) |
| **Source code truth** | git | — | yes | — |
| **Worker continuation state** | `.ai/runtime/task-<id>/handoff.json` | JSON | no | git + mechanical checkpoint |
| **Project configuration** | `.ai/project.yaml` | YAML | yes | authored |
| **Architectural contracts** | `.ai/architecture.md` + `.ai/contracts.lock` | MD + JSON | yes | authored + hashes |
| **Measured capabilities** | `.ai/capabilities.json` | JSON | no | re-probe |

Rules that follow:

- **The DB is never authoritative for anything a human wrote.** It is a cache of execution
  state over the spec. Deleting it must be survivable, and §4.3 defines how.
- **`tasks.yaml` is never authoritative for runtime facts.** It has no `status` field. Status,
  leases, attempts, spend and heartbeats exist only in the DB.
- **Nothing but git is authoritative for file contents.** The DB stores SHAs, never content.

### 4.2 `ownership.yaml` is deleted

v2 had *both* `ownership.yaml` (static module ownership) and task `owned_paths`/leases
(dynamic). Two authorities for "who may edit this file" is precisely the defect §4 exists
to eliminate — and in practice the static file would silently drift.

**Ownership is a property of a task, expressed as `expected_paths` in `tasks.yaml` and
enforced as a lease in the DB.** Long-lived module→owner mapping, where it is genuinely
wanted, belongs in `architecture.md` as prose for humans, with no enforcement meaning.

### 4.3 Reconciliation

`agentkit reconcile` runs at every orchestrator start, and is the only path that mutates
state on startup.

**Case: DB missing or corrupt.**
1. Re-create the schema.
2. Load `tasks.yaml`; insert every task as `PLANNED`.
3. For each task, look for `agent/task-<id>-*` branches and a worktree.
4. Read `handoff.json` if present; restore `base_sha`, `branch`, `worktree`, `attempts`.
5. Derive status conservatively: branch exists + commits ahead of base → `REVIEW`;
   branch exists + no commits → `READY`; already merged into integration → `DONE`.
6. **Grant no leases.** Every reconstructed task with a worktree becomes `STALE` and
   requires explicit `agentkit adopt <id>` or `agentkit discard <id>`.

The conservative step is deliberate: a rebuilt DB cannot know whether a worker is still
running, and inventing a lease is the one mistake that produces two agents in one file.

**Case: `tasks.yaml` edited while tasks are active.** Each task carries `spec_hash`. On
reconcile, a changed hash for a task not in a terminal state sets it to `NEEDS_REPLAN`,
suspends its lease, and reports the diff. Work already committed is untouched; the human or
architect decides whether to adopt, re-scope or cancel.

**Case: orchestrator crash.** Leases carry `expires_at` and are renewed by heartbeat. On
restart: any lease whose heartbeat is older than its TTL is expired, and its task becomes
`STALE` (§13). Any lease whose worker PID is still alive **and** whose heartbeat is fresh
is adopted as-is.

**Case: stale lease with a live process.** Never auto-revoke. Expire the lease, mark the
task `STALE`, and require `agentkit adopt` (reattach) or `agentkit kill` (terminate then
release). Overlapping workers is a worse outcome than a stalled one.

---

## 5. Task lifecycle

### 5.1 States

```
PLANNED ──► READY ──► LEASED ──► RUNNING ──► VERIFYING ──► REVIEW
                                                              │
                                              INTEGRATION_READY◄┘
                                                              │
                                                   INTEGRATING ──► DONE
```

| State | Meaning | Who may set it |
|---|---|---|
| `PLANNED` | in the spec, dependencies unmet | reconcile, architect |
| `READY` | dependencies met, no lease yet | scheduler |
| `LEASED` | lease granted, worker not yet started | scheduler |
| `RUNNING` | worker alive, heartbeat fresh | scheduler / adapter events |
| `VERIFYING` | worker finished, gates executing | core |
| `REVIEW` | gates passed, awaiting reviewer verdict | core |
| `INTEGRATION_READY` | reviewer PASS + audit clean | core |
| `INTEGRATING` | merge in progress | integrator |
| `DONE` | merged into integration branch | integrator |
| `BLOCKED` | waiting on an amendment or a human | any, with a reason |
| `FAILED` | gates or review rejected it | core |
| `STALE` | lease expired or worker lost | reconcile / supervisor |
| `NEEDS_REPLAN` | spec changed, or 3 failed attempts | reconcile / core |
| `CANCELLED` | withdrawn | human |

### 5.2 Legal transitions

| From | To |
|---|---|
| `PLANNED` | `READY`, `CANCELLED`, `NEEDS_REPLAN` |
| `READY` | `LEASED`, `BLOCKED`, `CANCELLED`, `NEEDS_REPLAN` |
| `LEASED` | `RUNNING`, `READY` (lease released), `STALE`, `FAILED`, `NEEDS_REPLAN` |
| `RUNNING` | `VERIFYING`, `BLOCKED`, `READY` (requeue), `STALE`, `FAILED`, `NEEDS_REPLAN` |
| `VERIFYING` | `REVIEW`, `RUNNING`, `READY`, `FAILED`, `NEEDS_REPLAN` |
| `REVIEW` | `INTEGRATION_READY`, `FAILED`, `NEEDS_REPLAN` |
| `INTEGRATION_READY` | `INTEGRATING`, `FAILED` |
| `INTEGRATING` | `DONE`, `FAILED` |
| `BLOCKED` | `READY`, `NEEDS_REPLAN`, `CANCELLED` |
| `FAILED` | `READY` (retry), `NEEDS_REPLAN`, `CANCELLED` |
| `STALE` | `RUNNING` (adopt), `READY` (discard), `NEEDS_REPLAN` |
| `NEEDS_REPLAN` | `PLANNED`, `CANCELLED` |
| `DONE`, `CANCELLED` | — terminal |

Any other transition is rejected and logged as `illegal_transition`. **Agents cannot set
status directly**; they call `task_status`, which validates against this table. Hooks may
only set `VERIFYING` and `STALE`. This is what stops three components inventing four
vocabularies.

Two edges deserve their reasons stated, because both were missing from the first draft of
this table and the acceptance suite caught it:

* **`RUNNING → READY` (requeue).** A worker that stops without finishing is requeued, not
  failed. The common cause is a provider outage, and routing that through `FAILED` would
  both misreport it and burn a retry (§13, class 9).
* **`RUNNING → NEEDS_REPLAN`.** §4.3 requires a spec edited mid-flight to stop the task
  immediately. Without this edge the status change is silently refused and the task drifts
  on against a specification nobody approved.

---

## 6. Lease semantics

### 6.1 Shape

```yaml
lease:
  id: L-1187
  task: T103
  worker: worker-7                    # adapter instance id, not a vendor name
  mode: exclusive-write               # exclusive-write | shared-read | advisory
  paths:
    - services/media/**
    - tests/media/**
  granted_at: 2026-09-13T12:00:00Z
  expires_at: 2026-09-13T16:00:00Z    # hard ceiling
  heartbeat_at: 2026-09-13T12:41:07Z
  expires_after_without_heartbeat: 10m
  generation: 3                       # bumped on every re-grant; see §14
```

### 6.2 Rules

| Aspect | Rule |
|---|---|
| **Owner** | exactly one task; a task may hold several leases |
| **Modes** | `exclusive-write` blocks all others; `shared-read` blocks writers, permits readers; `advisory` records intent only (used for prediction, §7) |
| **Nesting** | most specific match wins for *reporting*; for *authorisation* any matching foreign exclusive-write denies. An `exclusive-write` on `services/**` blocks a child claim on `services/media/x.py` |
| **Conflict** | two `exclusive-write` leases whose patterns overlap (§7.1) are never both granted |
| **Renewal** | any heartbeat renews `heartbeat_at`; `expires_at` is never extended — a task needing longer must re-request, which forces a human or architect look |
| **Heartbeat** | every worker event, and at minimum every 2 min from the supervisor; missing for `expires_after_without_heartbeat` → lease expired |
| **Expiry** | expired lease → task `STALE`, worktree and branch **preserved**, no auto-revoke of a live process |
| **Revocation** | `agentkit lease revoke <id> --reason` is a human action. It terminates the worker, quarantines the branch, and records a `lease_revoked` event |
| **Release** | on `DONE`, `FAILED` or `CANCELLED`; releasing re-evaluates `READY` for dependents |

### 6.3 When a worker needs a path it does not own

The protocol is mandatory and is enforced by the fact that the edit is blocked or detected
anyway:

1. **Stop modifying.** Do not seek another route to the same change.
2. Call `lease_request(paths, reason)` — or `graph_amend(proposal)` if the task itself is
   wrong rather than merely too narrow.
3. **Explain why** in the reason: which requirement of the task needs it.
4. **Wait.** Continue with in-scope work, or set `BLOCKED` and stop.

Silent scope expansion is the one behaviour the whole system exists to prevent. A
`lease_request` is cheap and informative; an out-of-scope edit is a merge-time rejection at
best and someone else's lost work at worst.

---

## 7. Overlap prediction

Blocking a collision at edit time is late — the two workers have already spent tokens.
v3 predicts overlap **before launch** and serialises rather than gambling.

### 7.1 Predicted modification set

Each task declares intent, which the architect fills in and the scheduler refines:

```yaml
expected_paths:
  write:
    - services/media/providers/**
    - tests/media/**
  read:
    - contracts/**
    - services/media/base.py
```

The scheduler computes `predicted_write(T)` from five sources, and records which
contributed (this is what makes a serialisation decision explainable in §15):

| Source | Signal | Confidence |
|---|---|---|
| `expected_paths.write` | declared | high |
| Import graph | files importing a declared target, for signature-changing tasks | medium |
| Git co-change | files historically changed in the same commit as a declared target | medium |
| Similar past tasks | path sets of completed tasks with similar scope | low |
| Generated-file map | outputs whose inputs are in the write set | high |

Co-change is the one worth defining precisely, because it catches couplings no static
analysis sees:

```
cochange(a, b) = commits_containing_both(a, b) / commits_containing(a)
coupled(a, b)  = cochange(a,b) >= 0.30 and commits_containing_both >= 3
```

### 7.2 The decision

```
can_run_together(T1, T2):
    W1, W2 := predicted_write(T1), predicted_write(T2)
    if overlaps(W1, W2):                       return SERIALIZE("predicted write overlap")
    if overlaps(W1, predicted_read(T2)) and T1 changes a contract:
                                               return SERIALIZE("reader of a changing contract")
    if confidence(W1) < HIGH or confidence(W2) < HIGH:
        if shared_module_ancestor(W1, W2):      return SERIALIZE("low confidence, same module")
    return PARALLEL
```

**When in doubt, serialise.** A false serialisation costs some wall-clock time; a false
parallel costs a corrupted merge and a human afternoon. The asymmetry is not close.

---

## 8. Hotspot model

### 8.1 Signals

v2 used `churn × log(lines) × fan_in`, which conflated collision risk with blast radius
and let a small, widely-imported helper outrank an 8,000-line file every feature touches.
v3 keeps two separate axes and adds the signals that predict *serialisation* specifically.

| Signal | Definition | Axis | Required? |
|---|---|---|---|
| `churn` | commits in 90 days | collision | yes |
| `size` | `log2(lines)` | collision | yes |
| `cochange_breadth` | distinct files coupled to it (§7.1) | collision | yes |
| `feature_spread` | distinct feature branches / commit-type prefixes touching it | collision | yes |
| `conflict_history` | merges where it appeared in the conflicted set | collision | optional |
| `fan_in` | files importing it | blast | yes |
| `centrality` | betweenness in the import graph | blast | optional |
| `task_pressure` | open/planned tasks whose `expected_paths` include it | pressure | yes |
| `char_safety` | test coverage of the file, 0–1 | risk modifier | optional |

### 8.2 Score

Deliberately simple — this ranks a queue, it does not need to be a model:

```
collision = churn × log2(lines) × (1 + cochange_breadth/10) × (1 + feature_spread/5)
blast     = 1 + sqrt(fan_in)
pressure  = 1 + task_pressure
score     = collision × blast × pressure
risk      = score × (2 − char_safety)          # untested hotspots are worse
```

### 8.3 Classification and how the scheduler uses it

| Class | Condition | Scheduler behaviour |
|---|---|---|
| `HOTSPOT` | `collision ≥ 200` or in the top 1% of `score` | exclusive-write lease required; never two tasks concurrently; requires `strong_write_isolation`; decoupling task proposed before fan-out |
| `MODERATE` | `collision ≥ 60` | `expected_paths` mandatory; L5 audit after every tool batch; serialise on any predicted overlap |
| `SAFE_PARALLEL` | otherwise | normal lease rules |

Classification is recomputed on every `agentkit plan` and cached in the DB with the commit
SHA it was computed at, so a stale classification is visible rather than silent.

**Measured on a pilot repository** (real output, v3 scoring): two large service modules
classify HOTSPOT ahead of the largest router; a 439-line `database.py` with 91 importers
classifies MODERATE with high blast — a file no size-based eyeball would have flagged.

---

## 9. Checkpoints: mechanical and semantic

v2's checkpoint depended on a context-exhausted model writing a good summary — the least
reliable actor at the least reliable moment. v3 splits it.

### 9.1 Mechanical checkpoint — no model involvement

Collected by core from git and the DB. Cannot fail for reasons of model judgement:

```json
{
  "kind": "mechanical",
  "task": 103, "generation": 3,
  "branch": "agent/task-103-media-providers",
  "worktree": "../wt-task-103",
  "base_sha": "ae81c61", "head_sha": "3f90b12",
  "commits_since_start": ["3f90b12 add veo provider", "9a1c004 registry entry"],
  "dirty_files": ["services/media/providers/veo.py"],
  "staged_files": [],
  "lease": { "id": "L-1187", "paths": ["services/media/**"], "mode": "exclusive-write" },
  "gates_run": [ { "level": "fast", "at": "12:40:02Z", "passed": false,
                   "failing": ["tests/media/test_veo.py::test_timeout"] } ],
  "audit": { "violations": [], "checked_at": "12:41:07Z" },
  "dependencies": { "blocked_by": [], "blocks": [107, 108] },
  "budget": { "spent_usd": 1.82, "cap_usd": 3.00 },
  "attempts": 2
}
```

Written on: every commit, `PreToolUse` batch boundaries, `PreCompact`, `Stop`,
`SubagentStop`, lease renewal, and on a 5-minute timer while `RUNNING`.

### 9.2 Semantic checkpoint — best effort

```json
{
  "kind": "semantic", "task": 103, "at_head": "3f90b12",
  "goal": "Add veo and ltx23 providers behind MediaProvider",
  "completed": ["MediaProvider protocol satisfied for veo", "registry entry added"],
  "remaining": ["ltx23 provider", "timeout handling in veo"],
  "decisions": ["Providers normalise to MediaResult so registry needs no branching"],
  "assumptions": ["veo returns a URL, not bytes — confirmed against contracts/media.yaml"],
  "blockers": ["ltx23 credentials not in the test environment"],
  "next_action": "Implement ltx23 provider mirroring veo, then run the fast gate",
  "important_files": ["services/media/base.py", "services/media/providers/veo.py"]
}
```

Requested before compaction and before termination. It is allowed to fail — timeout,
crash, exhausted context — and failure is recorded, not fatal.

### 9.3 Recovery sequence — exact

When a replacement worker starts on task T:

1. Read the **mechanical** checkpoint. If absent, rebuild it from git + DB directly
   (branch, `merge-base`, `git log base..HEAD`, `git status`). This step cannot fail while
   git is intact.
2. Verify the worktree matches `head_sha`. If it does not (crash mid-write), record
   `worktree_drift` and continue with the real state — git is the authority, not the file.
3. Read the **semantic** checkpoint if present *and* `at_head == head_sha`. A semantic
   checkpoint from an older commit is stale: use it for `decisions` and `assumptions` only,
   and discard `completed`/`remaining` as unreliable.
4. If no usable semantic checkpoint: **reconstruct** from mechanical state —
   `completed` ← commit subjects since `base_sha`; `remaining` ← task description minus
   what the diff already covers; `next_action` ← "review the diff since `base_sha`, then
   continue"; failing tests from `gates_run` become explicit blockers.
5. Re-assert the lease at a new `generation`. Never reuse the previous generation (§14).
6. Emit the brief (§5 of v2, unchanged) with a `reconstructed: true` flag so the worker
   knows its history is inferred rather than reported, and verifies before trusting it.

The property that matters: **step 4 always produces a workable brief.** The semantic layer
improves quality; it is never required for correctness.

---

## 10. Worktrees and environment isolation

### 10.1 The v2 defect

v2 recommended `symlinkDirectories: ["node_modules", ".venv", ".cache"]`. Sharing a mutable
dependency directory across worktrees is a correctness bug: one worker running
`npm install` or `uv sync` silently mutates every other worker's environment, and the
resulting failures appear in *unrelated* tasks. Disk was saved at the cost of the isolation
the worktree existed to provide.

### 10.2 Environment fingerprint

```
env_fingerprint = sha256(
      uv.lock | poetry.lock | requirements*.txt
    + package-lock.json | pnpm-lock.yaml | yarn.lock
    + pyproject.toml [dependency sections]
    + runtime versions (python --version, node --version)
    + platform tuple
)
```

### 10.3 Policy

| Artifact | Shared? | Why |
|---|---|---|
| Package **download caches** (`~/.cache/uv`, npm cache, pip cache) | **always shared** | content-addressed and immutable; concurrent-safe by design |
| `.venv`, `node_modules` — fingerprints match, and no active task has a lockfile in `expected_paths.write` | **shared, read-mostly** | identical by construction; nothing will mutate them |
| `.venv`, `node_modules` — fingerprints differ | **never shared** | different dependency graphs |
| Any worktree whose task *may modify dependencies* | **never shared** | it is the mutator; isolate it |
| Build output (`dist/`, `target/`, `Intermediate/`) | never shared | concurrent writers, no benefit |

The second clause is the important one, and it is checked at *lease* time, not at worktree
creation: a task that acquires a lease covering a lockfile immediately loses its shared
environment and is re-provisioned with a private one.

### 10.4 Tradeoff

| Strategy | Disk | Setup time | Risk |
|---|---|---|---|
| Full copy per worktree | high (GB per worker) | high | none |
| Shared cache + private env | moderate | low (cache warm) | none |
| Shared env, fingerprint-gated | low | lowest | none while the gate holds |
| Shared env, ungated (**v2**) | low | lowest | **silent cross-task corruption** |

Default: **shared cache + private env**, upgrading to a shared env only when the
fingerprint gate passes. On a large application repo the cache sharing recovers most of the
cost, and `sparsePaths` handles the source tree.

---

## 11. Git integration

### 11.1 Topology

```
main
 └── integration/<package>
       ├── agent/task-101-media-abstraction    (HOTSPOT, exclusive)
       ├── agent/task-102-provider-veo         (SAFE_PARALLEL)
       └── agent/task-103-provider-wan22       (SAFE_PARALLEL)
```

### 11.2 The pipeline

```
worker gate (fast) ──► lease audit (L7) ──► reviewer verdict ──► branch accepted
      ──► rebase onto integration ──► re-audit ──► merge --no-ff
      ──► full combined suite ──► cross-feature regression ──► human approval ──► main
```

The **combined** run is the point. Two individually green branches can be jointly broken,
and only the integration branch can discover it.

### 11.3 Rebase vs merge

- **Rebase** the worker branch onto integration before merging, to keep history linear and
  the audit diff small.
- **Merge `--no-ff`** into integration, so the worker's commits remain identifiable for
  post-hoc audit.
- Rebase rewrites SHAs, so the lease audit runs **twice**: on `base..head` pre-rebase, and
  on the rebased range. A discrepancy in the changed-file set between the two means the
  rebase absorbed something — stop and report.
- Never rebase `main`. Never rebase a branch another worker is on.

### 11.4 File classes needing special handling

| Class | Rule |
|---|---|
| **Migrations** (`alembic/versions/**`) | At most one open task may hold a migration lease. Merge order is the migration order; a second migration created concurrently is rejected and re-generated after the first lands |
| **Lockfiles** | Single owner. Never merged textually — regenerated on the integration branch and verified to be a no-op against each contributor |
| **Generated files** (`*.generated.*`, OpenAPI clients, protobuf) | Excluded from lease audit (§2.4) and from merges. Regenerated on the integration branch; if regeneration produces a diff, the *source* change was incomplete — reject |
| **Contracts** | Only `CONTRACT_CHANGE` tasks may touch them (§12) |
| **Schema changes** | Always `CONTRACT_CHANGE` + a migration lease |
| **Tests** | Merge freely; the combined suite is what counts, never the per-branch result |
| **Formatting-only churn** | Rejected in review — it inflates conflicts for no behavioural gain |

### 11.5 Integration order

Topological by dependency, then **widest lease first**. Merging the broadest change first
surfaces conflicts while the queue is short rather than after five narrow branches have
each been rebased.

---

## 12. `CONTRACT_CHANGE`

### 12.1 Why it is its own type

A contract change is not a normal task: its blast radius is every task that codes against
it. Treated as `SAFE_PARALLEL`, three workers implement three readings of an interface that
is still moving — the classic failure of parallel frontend/backend work.

### 12.2 Lifecycle

```
PROPOSE ──► FREEZE (human or architect approval) ──► FAN-OUT ──► IMPLEMENT ──► VERIFY
```

1. **Propose.** The task produces the contract only — schema, interface, event shape — and
   nothing that consumes it. Its lease covers `contracts/**` exclusively.
2. **Freeze.** On approval, core writes `.ai/contracts.lock`:

   ```json
   { "version": 7,
     "frozen_at": "2026-09-13T12:00:00Z",
     "paths": { "contracts/media.yaml": "sha256:1f3a…",
                "packages/api-contracts/src/media-v1.generated.ts": "sha256:9cb2…" } }
   ```
3. **Fan out.** Dependent tasks are created with `contract_version: 7` and a
   **`shared-read` lease** on the contract paths. Reading is allowed; writing is denied by
   `authorize_write` with the reason naming the freeze.
4. **Implement.** Workers build against version 7. Any attempt to modify a frozen contract
   path is blocked at L3/L4 and rejected at L7.
5. **Verify.** The merge gate re-hashes every frozen path. A hash mismatch on a task that
   is not the owning `CONTRACT_CHANGE` is an automatic reject.

### 12.3 Changing a frozen contract

Not possible from a dependent task, by construction. It requires a **new**
`CONTRACT_CHANGE` task, which sets every dependent task holding the old version to
`NEEDS_REPLAN`. That cost is intentional: it makes contract churn visible, which is the
only thing that discourages it.

---

## 13. Failure taxonomy

One generic "restart the agent" causes two specific harms: it repeats work that already
succeeded, and it retries tasks that are wrong rather than unlucky.

| # | Failure | Detection | Recovery | Escalation |
|---|---|---|---|---|
| 1 | **Agent process crash** | PID gone, exit ≠ 0 | mechanical checkpoint is current; relaunch with `resume_session` if capable, else replay from checkpoint. Same worktree, new `generation` | 3 crashes → `NEEDS_REPLAN` |
| 2 | **Context exhaustion** | `PreCompact` fired, or context-limit event | let compaction proceed; semantic checkpoint was written pre-compaction. No restart | repeated in one task → split the task |
| 3 | **Stalled worker** | no event for N min, heartbeat stale, tree unchanged | lease expires → `STALE`. Preserve worktree. Require `adopt` or `discard` | never auto-kill a live process |
| 4 | **Repeated gate failure** | `attempts ≥ 3` on the same gate | **stop retrying.** `NEEDS_REPLAN` with the three failure outputs attached; architect re-evaluates scope and contract | human if architect cannot re-scope |
| 5 | **Repeated wrong implementation** | reviewer `REJECT` ×2 | `NEEDS_REPLAN`. Wrong twice is a specification problem, not an execution one | human |
| 6 | **Dirty worktree after crash** | `head_sha` ≠ worktree state | do **not** auto-clean. Snapshot the dirty diff into the checkpoint, then let the replacement worker decide: git is the authority, the dirt is evidence | human if the diff is large |
| 7 | **Stale lease** | heartbeat expiry | expire lease, `STALE`, worktree preserved, no auto-revoke | human `revoke` |
| 8 | **Budget exhausted** | `budget_cap` event or cost tally | finish current tool call, checkpoint, stop cleanly. `BLOCKED` with reason `budget` | human raises cap or re-scopes |
| 9 | **CLI / API failure** (auth, rate limit, outage) | adapter error class | **not a task failure.** Requeue `READY`, do not consume an attempt, retry with backoff. If capability-specific, re-probe (§3.3) | persistent → mark adapter unavailable, reschedule onto another |
| 10 | **Merge conflict — mechanical** | integrator | resolve imports/adjacent additions only; re-run full gate | — |
| 11 | **Merge conflict — semantic** | integrator judgement needed | **stop.** Both tasks → `REVIEW`, report the pair. This is a planning failure: §7 should have serialised them | architect, then human |
| 12 | **Environment/dependency failure** | gate fails with import/install error, not test error | re-provision the worktree environment (§10) before counting an attempt | 2 failures → isolate env permanently |
| 13 | **Orchestrator restart** | startup | `agentkit reconcile` (§4.3) | — |
| 14 | **Lease violation detected** (L5/L6/L7) | audit | quarantine the branch, `FAILED`, record the violating paths and channel. Never auto-fix | always surfaced to human |

Rule 9 is the one most systems get wrong: a rate limit is not the task's fault, and burning
an attempt on it eventually pushes a healthy task into `NEEDS_REPLAN`.

---

## 14. Idempotency

`agentkit run` must be safe to execute twice, concurrently, at any moment.

| Operation | Idempotency key | Mechanism |
|---|---|---|
| Launch a worker | `(task_id, generation)` | `UNIQUE(task_id, generation)` on a `worker_runs` row, inserted **before** spawn. Second attempt loses the insert and aborts |
| Grant a lease | `(task_id, generation)` | `UNIQUE` on active leases per task; re-grant bumps `generation` |
| Create a task | `spec_id` from `tasks.yaml` | upsert by `spec_id`, never by title |
| Commit | git | workers commit; core never commits on their behalf |
| Merge a branch | target SHA | skip if `git merge-base --is-ancestor <branch> <integration>` — already merged is a no-op, not an error |
| Create a worktree | path existence + `git worktree list` | adopt if present and on the right branch; else create |
| Provision an environment | `env_fingerprint` | marker file; skip if fingerprint matches |
| Write a checkpoint | `(task, kind, head_sha)` | upsert |
| Run a gate | `(task, level, head_sha)` | cached result; re-run only if HEAD moved |
| Install guards | content hash of the guard files | rewrite only on change |

**Generation** is the backbone: every re-lease, relaunch or recovery increments it. A
zombie worker from generation 2 that wakes up and calls `task_status` is rejected because
the task is at generation 3. That single rule removes an entire class of split-brain bugs.

A global advisory lock (`.ai/orchestrator.lock`, PID + start time) prevents two schedulers,
but correctness does not depend on it — the keys above hold even if it is lost.

---

## 15. Observability

### 15.1 `agentkit status`

```
TASK  KIND       STATUS      AGENT     MODEL   BRANCH                  LEASE            TESTS      $     RETRY  BLOCKER
103   SAFE_PAR   RUNNING     codex     gpt-5.6 agent/task-103-wan22    services/media/  fast:PASS  1.82  0      -
101   HOTSPOT    INTEGRATING claude    opus    agent/task-101-abstr    services/**      full:PASS  4.10  0      -
107   DEPENDENT  BLOCKED     -         -       -                       -                -          -     0      waits:101
109   SAFE_PAR   STALE       claude    sonnet  agent/task-109-export   (expired 12:31)  fast:FAIL  0.94  2      lease expired
```

Plus, per task on request: dependencies both ways, last event, last commit, worktree path,
lease generation, budget cap, and the current blocker with its cause.

### 15.2 Event log

Every state transition writes an event with enough context to be explained later:

```json
{ "at": "12:31:44Z", "task": 109, "kind": "lease_expired", "generation": 2,
  "cause": "no heartbeat for 11m (limit 10m)",
  "detail": { "last_event": "12:20:31Z", "worker_pid": 24118, "pid_alive": false },
  "effect": "status RUNNING -> STALE; lease L-1190 expired; worktree preserved" }
```

The four questions, each answerable by a single query:

| Question | Answered by |
|---|---|
| Why did AgentKit launch this worker? | `worker_launched` event: task, why it was `READY`, which capabilities matched its requirements, which overlap checks passed |
| Why was this worker stopped? | last terminal event: `budget_exhausted`, `lease_expired`, `gate_failed`, `crash`, with the measurement that triggered it |
| Why was this task serialized? | `serialization_decision` event: the overlapping path set, which prediction source produced it, and its confidence |
| Why did this merge fail? | `merge_rejected` event: violating paths, or the failing gate command with its output tail |

### 15.3 Cost and budget

Per task and in aggregate: spend, cap, tokens where the adapter reports them. A task
approaching its cap emits a warning event before the hard stop, so the checkpoint is
written while there is still budget to write it.

---

## 16. Human control points

Autonomy inside an approved scope; approval at boundaries where a mistake is expensive or
irreversible.

**Always requires a human:**

| Boundary | Why |
|---|---|
| `integration → main` | the last reversible moment |
| Destructive migrations (`DROP`, `TRUNCATE`, non-additive `ALTER`) | data loss is not recoverable by rerunning |
| Freezing a contract (§12.2) | commits every dependent task to it |
| Scope beyond the approved task graph | the plan was the thing approved |
| Any `lease_revoke` | terminates another agent's work |
| Secrets, credentials, deployment config | §17 |
| `agentkit adopt` on a `STALE` task | only a human can know whether the old worker is truly gone |
| Raising a budget cap | otherwise the cap is decorative |

**Never asks a human:** which name to use, how to structure a function, whether to add a
test, formatting, library idiom, or any choice recoverable by editing a file. Those are
reviewed in the diff, not prompted mid-task.

**Asynchronous by default.** Approvals queue as `BLOCKED` tasks with a reason rather than
blocking a terminal, so that stepping away pauses progress instead of stalling a worker
holding a lease.

---

## 17. Security boundaries

### 17.1 Always denied, every role

`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`, `.aws/`, `.ssh/`, `.npmrc`,
`.netrc`, cloud CLI config; `.ai/tasks.db`; `git push`; `git reset --hard` outside one's own
worktree; `git clean -xfd`; history rewrites.

Reads matter as much as writes: a secret read into context is a secret in a transcript, a
log, and possibly a provider's servers. Hence `Read(.env)` is denied, not just `Edit`.

### 17.2 Least-privilege profiles

| Capability | research | test-author | implementer | lead-impl | decoupler | reviewer | integrator |
|---|---|---|---|---|---|---|---|
| Read source | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Write source | — | — | ✔ scope | ✔ scope | ✔ scope | — | — |
| Write tests | — | ✔ | ✔ scope | ✔ scope | — | — | — |
| Run tests | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Arbitrary shell | — | limited | limited | limited | limited | — | limited |
| Install packages | — | — | — | ✔ ask | — | — | ✔ ask |
| Network | allowlist | — | allowlist | allowlist | — | — | allowlist |
| Docker | — | — | — | ask | — | — | ask |
| Migrations | — | — | — | ✔ lease | — | — | ✔ |
| `git commit` | — | ✔ | ✔ | ✔ | ✔ | — | ✔ |
| `git merge` | — | — | — | — | — | — | ✔ integration only |
| `git push` | — | — | — | — | — | — | ask, human |
| Secrets | — | — | — | — | — | — | — |

"limited" = the L4 shell guard with the project's gate commands allowlisted and
opaque-write commands denied.

### 17.3 Network and supply chain

- Sandbox network allowlist where the adapter supports it (`network.allowedDomains`,
  `strictAllowlist`), defaulting to the package registries and nothing else.
- Package installation is a lease-bearing action: it changes lockfiles, so it needs the
  lockfile lease (§10.3, §11.4) and cannot happen incidentally.
- **MCP servers are allowlisted by name.** A worker may not add one mid-session; an
  arbitrary MCP server is an unaudited write channel and an exfiltration path.
- Production databases and deployment targets are never reachable from a worker's
  environment. Workers get test credentials or none.

---

## 18. Acceptance suite — AgentKit proving its own guarantees

These run against a synthetic repository in CI and must pass before any real project is
onboarded. **A guarantee without a passing test here is downgraded to a hope in §2.3.**

| # | Test | Setup | Expected | Pass criterion |
|---|---|---|---|---|
| 1 | **Collision** | two tasks, overlapping lease, both edit `shared.py` via Edit | one succeeds, the other blocked pre-write | blocked worker made no change; `edit_blocked` event recorded |
| 2 | **Bash bypass** | worker uses `sed -i`, `> redirect`, `python -c`, `cp`, `git checkout -- path` on an unowned file | prevented where statically decidable; otherwise detected before commit | L4 blocks the decidable cases; L5 flags the rest within one batch; L6 refuses the commit; L7 refuses the merge. **Nothing reaches integration** |
| 3 | **Cross-worktree write** | worker writes an absolute path into another worktree | prevented where sandboxed; otherwise detected by the victim's audit | no change survives to integration; violation event names the channel |
| 4 | **Crash recovery** | SIGKILL a worker mid-task | lease goes stale safely; replacement resumes | no duplicate commits; replacement's first action derives from the checkpoint, not from scratch |
| 5 | **Context loss** | delete session history, then resume | reconstructed brief from git + checkpoint | task completes; `reconstructed: true` recorded; no work repeated |
| 6 | **Orchestrator restart** | SIGKILL core with 3 tasks running, restart | reconcile restores the graph | zero duplicate workers; zero duplicate leases; no task silently advanced |
| 7 | **Double run** | `agentkit run` twice concurrently | second is a no-op for in-flight work | `UNIQUE(task_id, generation)` rejects the duplicate launch; no duplicate commits |
| 8 | **Dependency order** | A → (B, C) → D | B and C only after A; D after both | ordering holds; B and C genuinely overlap in time |
| 9 | **Contract change** | contract task freezes v7; two dependents | dependents cannot modify the contract | write denied at L3/L4; frozen hashes verified at L7; a stale-version dependent goes `NEEDS_REPLAN` |
| 10 | **Failed integration** | two branches each green alone, broken together | integration gate catches it | `main` unchanged; both tasks returned; `merge_rejected` names the failing combined test |
| 11 | **Stale lease, live process** | freeze a worker past its heartbeat TTL | lease expires, process not killed | task `STALE`; worktree intact; no second worker granted the lease |
| 12 | **Rate limit** | adapter returns a rate-limit error | requeued, attempt not consumed | `attempts` unchanged; task returns to `READY`; backoff observed |
| 13 | **Environment isolation** | two worktrees, different lockfiles | environments not shared | fingerprints differ; each worktree resolves its own dependency set |
| 14 | **Capability downgrade** | probe reports `prewrite_file_guard: false` | HOTSPOT tasks are not scheduled onto it | scheduler assigns only `SAFE_PARALLEL`/`TEST_ONLY`; decision recorded |

Tests 2, 3 and 10 are the ones that distinguish this design from v2. Test 14 is the one
that keeps §3 honest.

---

## 19. Simplicity audit

Every component names the failure it prevents. Anything that could not name one was cut.

| Component | Failure prevented | Keep? |
|---|---|---|
| Git worktrees | two agents editing one working tree | **yes** |
| SQLite runtime state | losing execution state on restart | **yes** |
| MCP spine | per-vendor coordination protocols | **yes** |
| Pre-write hooks (L3/L4) | wasted work before the merge gate catches it | **yes** |
| Worktree audit (L5) | shell/codegen mutations going unnoticed | **yes** |
| Pre-commit validation (L6) | violations entering history | **yes** |
| Merge gate (L7) | out-of-lease change reaching integration | **yes — the only absolute guarantee** |
| Mechanical checkpoint | recovery depending on model goodwill | **yes** |
| Semantic checkpoint | re-deriving decisions and dead ends | **yes** (best-effort) |
| Generation counter | zombie workers acting on stale state | **yes** |
| Capability probe | vendor assumptions silently wrong (§0.3) | **yes** |
| Overlap prediction | discovering collisions after spending tokens | **yes** |
| `CONTRACT_CHANGE` | parallel work against a moving interface | **yes** |
| Event log | unexplainable behaviour | **yes** |
| `ownership.yaml` | *nothing* — duplicates leases | **cut** (§4.2) |
| Static role→vendor mapping | *nothing* — and was wrong (§0.3) | **cut** |
| Shared mutable env dirs | *negative* — caused cross-task corruption | **cut** (§10) |
| `http` hooks / dashboard | nothing yet | **deferred** until an operator asks |
| Message broker, Redis, K8s | nothing at this scale | **never** |
| `tasks.db` committed to git | nothing; creates merge conflicts in state | **never** |

Total moving parts: git, SQLite, MCP, worktrees, hooks/sandbox, tests, and one small Python
scheduler. That is the right size for one developer running several local agents.

---

## 20. Phased rollout

Each phase ships only when its acceptance tests pass. No phase begins before the previous
one has caught a real problem in real use.

| Phase | Deliverable | Gate to exit |
|---|---|---|
| **0. Adapters + probe** | detection, capability probe, `agentkit doctor` | probe correctly reports the known-false `worktree_native` on this Codex build (test 14) |
| **1. Core state** | tasks.yaml ↔ SQLite, state machine, reconcile | tests 6, 7 |
| **2. Enforcement L0–L7** | guards, audit, pre-commit, merge gate | tests 1, 2, 3, 11 — **the phase that must not be skipped** |
| **3. Checkpoints + recovery** | mechanical + semantic, recovery sequence | tests 4, 5 |
| **4. Scheduler** | dependencies, overlap prediction, leases | tests 8, 12 |
| **5. Integration** | integrator, combined gate, merge queue | test 10 |
| **6. Contracts** | `CONTRACT_CHANGE`, freeze, `contracts.lock` | test 9 |
| **7. Pilot** | onboard a real application repo; decouple the measured hotspots | two agents concurrently on two new modules, zero violations at L7 |
| **8. Second repo** | a repo on a different stack | `agentkit init` + gates is the entire adoption cost |

Phase 2 is the load-bearing one. **Do not build the scheduler until enforcement has blocked
something real** — a scheduler that distributes work a system cannot police just distributes
damage faster.

---

## v2 → v3 corrections

| v2 weakness | v3 correction | Why it mattered |
|---|---|---|
| "A worker physically cannot edit outside its lease" | Seven layers, each labelled PREVENTION / DETECTION / MERGE-TIME; the absolute guarantee is scoped to "nothing out-of-lease reaches integration" | The claim was false. Shell, interpreters, codegen and package managers all bypass a tool hook. A guarantee believed but untrue is worse than one known to be partial |
| Vendor-coded roles ("hotspots → Claude, safe-parallel → Codex") | Capability model + runtime probing; no vendor appears in core | The premise was wrong — Codex has `PreToolUse` with a block decision. Hardcoded vendor assumptions rot silently |
| Codex `--worktree` listed as available | Graded EXPERIMENTAL (flag is off by default); core creates worktrees itself | A version-specific, disabled feature had become an architectural dependency |
| `tasks.yaml` **and** `tasks.db` both describing tasks | tasks.yaml = spec (committed, no status); DB = runtime only; explicit reconcile | Two authorities for one fact guarantees eventual disagreement with no tiebreaker |
| `ownership.yaml` alongside leases | Deleted; ownership is a task property | Same defect, second instance |
| `symlinkDirectories: [node_modules, .venv]` | Fingerprint-gated sharing; immutable caches shared, mutable envs isolated | One worker's `npm install` corrupted every other worker, surfacing as failures in unrelated tasks |
| Checkpoint depended on the model writing a good summary | Mechanical (always) + semantic (best-effort) + a recovery sequence that works without the semantic layer | The least reliable actor was relied on at its least reliable moment |
| `churn × log(lines) × fan_in` | Separate collision and blast axes, plus co-change, feature spread and task pressure; three classes drive scheduling | The single formula ranked a 439-line helper above an 8,453-line file every feature edits |
| Collisions discovered at edit time | Overlap predicted before launch; serialise when confidence is low | Blocking after the work is done wastes the tokens the work cost |
| Ad-hoc task states across DB, hooks and agents | One state machine, legal transitions enumerated, agents cannot set status directly | Three components were inventing four vocabularies |
| Leases with a TTL and little else | Full semantics: modes, nesting, renewal, heartbeat, expiry, revocation, generation | "Stale lease" was undefined precisely where two workers could overlap |
| No idempotency story | Idempotency keys per operation; `generation` invalidates zombies | `agentkit run` twice would have duplicated workers and commits |
| Generic "restart the agent" | 14 failure classes, each with its own recovery; rate limits never consume an attempt | Retrying a mis-scoped task is how a system loops forever |
| Contract changes as ordinary tasks | `CONTRACT_CHANGE` with freeze + `contracts.lock` + read-only leases for dependents | Parallel frontend/backend work against a moving interface is the classic failure |
| Status output only | Event log answering why-launched / why-stopped / why-serialized / why-merge-failed | Unexplainable autonomy cannot be trusted or debugged |
| No self-verification | 14 acceptance tests gating each phase | Guarantees nobody tested are marketing |

---

## Architecture invariants

True regardless of which agents exist, which versions are installed, or which vendor
disappears next year.

1. **Git is the only authority for file contents.** Every other store holds SHAs, never code.
2. **No change outside a task's lease reaches the integration branch.** Enforced
   deterministically at merge time, with no model in the loop.
3. **Exactly one authority per fact.** Spec, runtime state, code, continuation state,
   configuration and contracts each have exactly one owner.
4. **One exclusive writer per path at a time.** Overlapping exclusive leases are never
   granted, under any recovery path, including a rebuilt database.
5. **Sessions are disposable; the repository is permanent.** Any worker can be destroyed at
   any instant, and a replacement continues from git plus the mechanical checkpoint alone.
6. **Recovery never invents authority.** A rebuilt database grants no leases and advances no
   task; ambiguity resolves to `STALE`, never to "probably fine".
7. **The core names no vendor.** Vendor knowledge exists only in adapters and in probed
   capabilities.
8. **Capabilities are measured, never assumed.** No behaviour keys off a version string.
9. **Every state transition is legal, logged, and explainable after the fact.**
10. **Every mutating operation is idempotent under an identity key**, so restart at any
    instant is safe.
11. **Prevention is claimed only where something is prevented.** Everything else is labelled
    detection, and detection is backed by a gate that cannot be bypassed.
12. **Uncertainty serialises.** When overlap confidence is low, tasks run in sequence.
13. **Contracts freeze before dependents fan out.**
14. **Humans decide what is irreversible.** Everything recoverable by editing a file is the
    agents' to decide.

---

## Production-readiness checklist

Objective pass/fail. AgentKit is not production-ready until every line passes.

### Enforcement
- [ ] Acceptance tests 1, 2, 3 pass — including every bypass channel in §2.1
- [ ] L7 rejects an out-of-lease branch with no model involvement
- [ ] L6 pre-commit hook installed per worktree; `--no-verify` denied at L2
- [ ] L5 audit completes in < 200 ms on the largest target repo
- [ ] Every §2.3 guarantee maps to a passing test; unmapped guarantees downgraded in the doc

### Capabilities
- [ ] `agentkit probe` measures all 12 capabilities functionally, not from version strings
- [ ] Probe re-runs automatically on adapter version change
- [ ] Scheduler refuses HOTSPOT work to agents lacking `strong_write_isolation` (test 14)
- [ ] Zero vendor identifiers outside `adapters/` — verified by a grep test in CI

### State and recovery
- [ ] Deleting `tasks.db` mid-run loses no committed work and grants no leases (test 6)
- [ ] `agentkit run` twice concurrently produces no duplicates (test 7)
- [ ] SIGKILL on a worker → `STALE`, worktree preserved, clean resume (test 4)
- [ ] Session history deleted → reconstructed brief, no repeated work (test 5)
- [ ] Editing `tasks.yaml` on an active task → `NEEDS_REPLAN`, work preserved
- [ ] Every operation in §14 has a tested idempotency key

### Concurrency correctness
- [ ] Overlapping exclusive leases provably impossible, including after reconcile (test 11)
- [ ] Dependency ordering holds and genuinely overlaps independent work (test 8)
- [ ] Low-confidence overlap serialises, with the decision logged (§15.2)
- [ ] Environment fingerprinting prevents cross-worktree dependency corruption (test 13)

### Integration
- [ ] Combined suite runs on the integration branch, not only per branch (test 10)
- [ ] Migrations, lockfiles and generated files follow §11.4
- [ ] Frozen contracts verified by hash at merge (test 9)
- [ ] `main` unreachable without human approval — verified by attempting it

### Operations
- [ ] `agentkit status` shows every §15.1 column
- [ ] All four "why" questions answerable from the event log alone
- [ ] Budget caps enforced; warning precedes the hard stop so the checkpoint survives
- [ ] Rate limits do not consume attempts (test 12)

### Security
- [ ] Secret paths denied for read *and* write, every role
- [ ] Role profiles match §17.2; a test agent cannot merge, an integrator cannot read `.env`
- [ ] MCP servers allowlisted; workers cannot add one mid-session
- [ ] Network allowlist active wherever the adapter supports it

### Honesty
- [ ] No STABLE claim in §0 without a passing runtime probe
- [ ] No EXPERIMENTAL or UNVERIFIED feature is load-bearing in core
- [ ] Every documented guarantee names its enforcement layer and its test
