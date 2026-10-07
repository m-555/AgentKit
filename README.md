# AgentKit

Coordinate Claude Code and Codex workers in a separate Git repository, from a
first runnable draft through changes to an established project.

A **job** stores the user's request, corrections, acceptance criteria and planning
decisions. Its **coordinator** stays on the chosen provider. The coordinator
inspects the current repository and defines **tasks** with file ownership,
dependencies, roles, skills and checks. A Python **supervisor** runs independently
of agent quotas and manages workers, reviews, recovery and integration.

Documentation starts at [docs/README.md](docs/README.md). It separates current
operating guides from historical plans, reviews and evaluation reports.
See [skills and project guidance](docs/skills-and-project-guidance.md) for how
AgentKit and repository-specific skills reach Claude and Codex.

## Start a job

Install from this checkout with Python 3.11+ and Git:

```sh
uv tool install --editable ./orchestrator
cd /path/to/target-repo
agentkit init
agentkit probe --functional
agentkit job create first-draft --request-file request.md
agentkit job start --max-workers 2
agentkit job status
```

To add the Claude Code plugin (agents, skills, commands and enforcement hooks):

```sh
claude plugin marketplace add m-555/AgentKit
claude plugin install agentkit@agentkit-local
```

Run `agentkit --version` to verify the checkout and installed package metadata.
For explicit model assignments, unlimited scheduling and manager recovery, see
[the recovery guide](docs/recovery-and-model-policy.md).
For Windows Claude workers see [WSL transport](docs/wsl-transport.md);
for stable paths and automatic merging see [worktree integration policy](docs/worktree-integration-policy.md).

The target must have an initial Git commit and a configured Git author. For a
new repository, initialize Git and commit a README before starting. Keep any
existing project changes committed so worker branches receive them. AgentKit
leaves the operator checkout on its current branch and preserves its local edits.

Functional probing launches small real agent requests. Static `agentkit probe`
only inspects available flags; it cannot certify filesystem confinement. Write
workers require a successful confinement probe. An unavailable sandbox is a setup
blocker, particularly for native Windows installations; changing a capability
boolean does not provide isolation.

Write `request.md` in product terms: desired behavior, constraints, examples,
acceptance criteria, and what is outside scope. You can instead talk to your main
agent through the AgentKit MCP server: it uses `job_create`, `job_decision`,
`task_define`, `job_plan_ready` and `job_start`. Supervised workers cannot change
the plan or approve their own work.

To add a correction without losing the original request:

```sh
agentkit job update first-draft --request-file correction.md
```

The revision changes and new task launches and merges wait for coordinator
replanning. Existing worker results remain available for inspection.

## Define work

Use one job for an outcome the user can accept. Split tasks by independently
reviewable changes and file ownership, with dependencies wherever interfaces or
shared files overlap. Skills are reusable instructions, not task status or queues.

For a new project, start with a small runnable foundation and its checks. Establish
shared interfaces with a `CONTRACT_CHANGE` task before dependent workers fan out.
For an existing project, inspect conventions, baseline tests and hotspots first;
use `DECOUPLE` only when a shared file prevents a useful split. Put implementation
and its focused tests in the same task unless a separate test task has clear
ownership and dependencies.

The coordinator saves definitions in `.ai/tasks.yaml`. For example:

```yaml
tasks:
  - id: calendar-store
    job_id: first-draft
    title: Persist calendar entries locally
    kind: SAFE_PARALLEL
    role: implementer
    expected_paths:
      read: [contracts/calendar.json]
      write: [src/calendar/store.py, tests/test_calendar_store.py]
    depends_on: [calendar-contract]
    gate_level: fast
    skills: [.ai/skills/local-storage.md]
    acceptance:
      - Entries survive a restart.
      - Invalid dates return an actionable error.
```

Skill entries accept project-relative instruction files or bundled skill names
such as `plan-feature`. Roles use bundled agent instructions or `roles` mappings
to instruction files in `.ai/project.yaml`. Unknown task kinds, invalid paths,
dependency cycles and undefined gates are rejected.

The coordinator may use `project_configure` to set real gates, setup commands,
contracts and hotspots after inspecting the project. Both a task gate and a
`full` gate are required. Checks must terminate without changing committed files.
[Setup profiles](docs/workspaces-and-environments.md) prepare only required tools
and reuse private npm snapshots or cached Python wheels. Legacy `worktree_setup`
builds private environments; `.venv` and `node_modules` are never
shared. On an empty project, avoid setup commands requiring manifests that the
first worker has yet to create.

## Model policy

| Role / worker preference | Model | Effort |
|---|---|---|
| Coordinator and reviewer | GPT-6 Astra; fallback Claude Opus 5.5 | high |
| Worker 1 (default) | GPT-6.1 Sol | high |
| Worker 2 (preferred fallback) | Claude Opus 5.5 | high |
| Worker 3 (easy tasks only) | Claude Sonnet 5 | high |
| Worker 4 (explicit easy research assignment) | Local Qwen 3.8 | local default |

This order is your configured preference, not a measured benchmark. The scheduler
also checks task complexity, runtime capabilities, model access and account quota.
The coordinator can set `complexity: easy` and `model_profile: qwen` (or `sonnet`)
on bounded tasks. Standard and complex work stays with Sol or Opus. A failed
small-model attempt is promoted to a preferred worker. One local GPU worker runs
at a time. Qwen is currently limited to `RESEARCH`: OpenCode tool permissions do
not establish the filesystem isolation required for unattended source edits.

New jobs default to automatic initial selection: Astra, then Opus at high. Once
the coordinator starts working, its exact provider, model and effort are pinned
in durable job memory. Quota exhaustion waits for that coordinator; eligible
workers and independent reviewers continue. An explicit `--coordinator codex`
or `--coordinator claude-code` restricts initial selection to that provider.

`agentkit models` shows the policy and saved availability. New projects include
`model_policy.profiles` in `.ai/project.yaml`; existing projects inherit the same
defaults. This supersedes the old `models.<role>` and `provider_models` mappings.
Use `model_policy.roles` for exact coordinator/reviewer pins and named
`model_policy.assignments` for worker choices and approved failure-triggered
fallbacks. A task opts in with `model_assignment`; explicit choices do not silently
upgrade or switch provider. See [the complete example](docs/recovery-and-model-policy.md).

Profile settings support `model`, `effort` and `enabled`; control effort stays
`xhigh`, and edits do not change an already pinned coordinator.

The catalog was verified on 2026-09-30: workers now use `gpt-6.1-sol` and
`claude-opus-5-5`. Existing project defaults naming `gpt-5.6-sol`, `gpt-6-sol`
or `claude-opus-5` resolve to these approved replacements on their next launch.
Set `pinned: true` alongside an explicit model ID to retain that version. New
project templates inherit model IDs from the central catalog. Unknown snapshots
and an active job's pinned coordinator are preserved. This is a verified upgrade
list, not automatic adoption of every new model announced by a provider.
`agentkit models --json` includes the verification date and superseded IDs.
See the official [Sol 6.1 model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [Opus 5.5 model documentation](https://platform.claude.com/docs/en/models/opus-5-5/overview).

The local adapter reads only the `local` provider from a sibling `local-opencode`
repository's `opencode.json`. Set `AGENTKIT_LOCAL_OPENCODE` to that repository's
path when installed elsewhere. It uses `local/qwen3.8-27b-q8-tuber` and requires
your existing router to be running on `127.0.0.1`. It does not modify the local
repository, load its coordinator workflow, or start another GPU model server.
Use `agentkit providers check` for runtime availability and `agentkit probe` for
capability discovery. Automated repository tests simulate models; they do not
prove live model access or intelligence.

Qwen now has a [live evaluation and assignment guide](docs/evaluations/QWEN_EVALUATION.md), including
actual edits and generated tests in disposable repositories. It passed bounded
code-reading and coding tasks; strict JSON formatting needs validation, and
production writes still require OS confinement. OpenCode logs, state and caches
are now scoped to `.ai/runtime/local-opencode` instead of the user's global data.
To repeat the opt-in local inference tasks, start the existing router and run
`python evals/local_qwen.py` from the `orchestrator` environment. Results stay under
the ignored `.test-artifacts` directory; this does not run during ordinary tests.

## Automatic lifecycle and quota recovery

```text
User request -> pinned coordinator -> saved task graph
  -> isolated workers -> task checks -> independent review of exact commit
  -> integration worktree -> combined full checks -> coordinator job acceptance
```

The supervisor records process ownership, session IDs, generations, heartbeats,
checkpoints and redacted event logs. Closing the initiating agent session does
not close the background supervisor. Restart it with `agentkit job start` after
a machine restart; OS login/startup service installation is not included.

Provider availability is shared by account. Five-hour and weekly windows are
stored separately when supplied. Codex uses documented app-server account quota
metadata. Claude uses available quota events and a bounded tool-free availability
request, which can consume a small amount of allowance. Checks are coalesced per
provider, with a default five-minute healthy polling interval configurable through
`availability_poll_seconds`.

A reset timestamp permits another check; it never counts as proof of availability.
A reported weekly block remains effective even if the shorter window resets.
When a provider omits a reset timestamp, AgentKit records uncertainty and retries
with backoff rather than inventing an exact time. An authoritative successful
check permits continuation.

Workers can resume the same session. Explicitly assigned workers transfer only
to an approved fallback for an allowed failure class, after old-owner termination
and preserved-work checks; a transferred owner stays selected after the original
provider recovers. Legacy worker preferences retain their ranked selection.
A new session does not reset an account's allowance. The coordinator's
provider never changes. Its original session is resumed when possible; an invalid
session is replaced on the **same provider**, loading durable job memory. Other
workers and approved reviewers continue under the existing plan while it waits.
Reviews use the explicit reviewer role pin when configured; otherwise they use
ranked defaults. Repeated `--reviewer` options restrict the provider list.

A returning manager must acknowledge an unchanged recovery audit before
integration and new dependent launches continue. An attached external manager
requires a live bridge and lease; confirmed bridge termination permits a CLI
replacement on the same pin. This does not wake the original native chat.

If the pinned manager's account stays unavailable, the user can appoint another
control profile with `agentkit manager repin JOB --profile NAME --evidence-file
decision.md`. It is refused while the old manager's lease is fresh or a session
still owns the job. It records the decision in the job and opens a new recovery
epoch that the new manager must audit and acknowledge. `agentkit manager handover
JOB --holder NAME --pid PID` moves an attached manager lease to the user's next
chat session on the same pin; the previous credential stops working.

Quota waits do not use task failure attempts or ask the human to intervene.
Authentication problems and genuinely missing product decisions remain explicit
blockers. Repeated task failures return to the coordinator for inspection.

An independent PASS applies only to the exact clean worker commit. Integration
audits scope, merges that commit in a dedicated checkout, runs the combined full
gate, and rolls back a failed merge. Contract changes freeze a version before
dependent work starts. The job becomes DONE only after coordinator acceptance
and another passing combined gate.

**Automated merges target the integration branch**, not protected branches such
as `main` or `master`. This preserves the repository's existing publication boundary.

## Operate and inspect

```sh
agentkit run --watch --max-workers 2   # foreground alternative
agentkit live --follow               # redacted agent and manager activity
agentkit status
agentkit providers status
agentkit providers check
agentkit events --task 7
agentkit why merge-failed 7
agentkit integrate                     # manually process approved merge queue
agentkit usage                         # project-wide recorded tokens; calls no model
agentkit recovery status               # pending recovery and wake intents
python -m agentkit.dashboard --root . --open   # local web dashboard, read-only by default
```

The dashboard's **Live agent screens** mirror registered Windows console windows.
[Live monitor](docs/live-monitor.md) covers project usage totals and opt-in line
input to an interactive CLI manager (`agentkit terminal register-manager`).

Commit `.ai/project.yaml`, `.ai/tasks.yaml`, `.ai/jobs/*.json` and project skills
as project records. SQLite runtime state, quota windows and process logs live
under `.ai/` and are ignored by Git. Preserve runtime state to preserve approvals
and sessions. A rebuild from Git alone requires re-auditing recovered branches;
it cannot reconstruct missing approval evidence or prove process ownership.

The supervisor log is `.ai/runtime/supervisor.log`; individual agent streams are
`.ai/runtime/process-<id>/events.jsonl`. `agentkit run --dry-run` avoids provider
checks and launches but may reconcile local runtime bookkeeping.

## Development and verification

```sh
cd orchestrator
uv sync --extra dev
uv run pytest -q  # use an external temporary directory; see docs/development.md
uv run ruff check agentkit tests
uv run mypy agentkit
```

Tests use temporary Git repositories, simulated provider responses and real
monitor/child processes. They do not certify current paid-provider behavior.
Live functional probing is required for each installed runtime and environment.
Hooks and audits are layers of enforcement, not a replacement for filesystem
confinement against writes into other worktrees.

See [GLOSSARY.md](docs/GLOSSARY.md) for terminology and [PLAN_V3.md](docs/plans/PLAN_V3.md) for the
historical design rationale. This README describes the current supervised workflow.

Use the configurable [separate-task workflow](docs/efficient-workflow.md) for
fresh-session builders and independent testers. Existing projects retain legacy
behavior until the operator enables this mode and replans incompatible tasks.
The localhost Team setup exports explicit effort choices for a reviewed profile;
selecting a value does not change a running session or an existing manager pin.

## Human or AI review

[Review workflows](docs/review-workflows.md) document the two selectable policies,
one planner/manager, structured user job files, automatic tester handoffs, bounded
fresh worker sessions, role model/effort choices and local human approval controls.
The dashboard is read-only by default; operator controls save decisions and idle
project settings without starting AI jobs.

## Recent changes

Since 2026-10-04:

**Features**

- **Manager re-pin and handover:** `agentkit manager repin` and `agentkit manager handover`
  (see [automatic lifecycle](#automatic-lifecycle-and-quota-recovery)).
- **Project usage:** `agentkit usage` totals the recorded tokens of every launch without
  calling a model; `--backfill` saves receipts from older stopped logs.
- **Live agent screens:** the dashboard mirrors registered Windows console windows, and an
  interactive CLI manager can opt into one-line input.
- **Shared recovery and wake:** durable recovery intents for CLI and editor managers,
  inspected with `agentkit recovery status|capabilities|cancel` and shown in the dashboard.
- **Preserved failing tests:** a manager can hold an independent failing test with
  `test_hold`; once the source fix is accepted, `test_refresh` carries the byte-identical
  test forward instead of paying another session to rewrite it.
- **Bounded manager queries:** `job_brief` pages job history to keep manager context small
  ([manager efficiency](docs/operations/manager-query-efficiency.md)).
- **Private browser checks:** Playwright gates test the assigned worktree through its own
  server and port ([browser checks](docs/browser-checks.md)).

**Fixes**

- A session launched for one task can read only that task's brief.
- Local Qwen workers read only the files their task declares; glob, grep and directory
  listings are denied.
- A local worker's standing task rules survive context compaction, and worker prompts
  state that the host normalizes line endings.
- Every worker provider commits through the host's `task_commit` tool.
- A monitor-side fault, such as a locked database during a heartbeat, no longer stops a
  healthy worker or uses one of its attempts.
- Recovery evidence and Git merges are gathered outside database write transactions, so
  other writers no longer time out on large projects.
- Operator leases stay alive across supervisor passes.
- A retry whose stopped attempt preserved nothing starts fresh instead of receiving a
  continuation packet.
- Windows process checks use complete PID snapshots and process identity, so a recycled
  PID is not mistaken for a live worker.
- Quota recovery handles manual resets, fences stale native returns and retries confirmed
  capacity failures after the quota wakes.

## License

MIT. See [LICENSE](LICENSE).
