# AgentKit

Coordinate Claude Code, Codex and configured local Qwen workers in a separate
Git repository, from a first draft through changes to an established project.

A **job** stores the user's request, written in their own words with screenshots
when they help, plus corrections, acceptance criteria and decisions. An
**assigner** (the coordinator) inspects the repository and turns the job into
**tasks** with file ownership, dependencies, roles, skills and checks. It plans
once, and again only when the user changes the job. **Builders** do the tasks,
and every task must pass its own checks. A **review** follows: per task, once per
job, by the user, or not at all. Code accepts the finished job. A Python
**supervisor** runs independently of agent quotas and manages workers, reviews,
recovery and integration.

When something stops, it becomes a **problem** for the user while the rest of the
job keeps going. The cheapest reply that fixes it can come from the view, the
terminal or a phone, by email.

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

Or run `agentkit view`, open the **Jobs** tab and write the job there, with up to
10 screenshots (PNG, JPEG, GIF or WebP, 8 MB each). Workers can open them through
the read-only `job_attachment` tool.

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
blocker, particularly for native Windows installations, where Claude Code has no
OS sandbox; run Claude writers in WSL. Changing a capability boolean does not
provide isolation.

Write `request.md` in product terms: desired behavior, constraints, examples,
acceptance criteria, and what is outside scope. You can instead talk to your main
agent through the AgentKit MCP server: it uses `job_create`, `job_decision`,
`task_define`, `job_plan_ready` and `job_start`. Supervised workers cannot change
the plan or approve their own work.

To add a correction without losing the original request:

```sh
agentkit job update first-draft --request-file correction.md
```

The revision changes, and new task launches and merges wait until the assigner
has re-planned. Existing worker results remain available for inspection.

## Workflow and review

`agentkit init` writes this for a new project:

```yaml
workflow:
  mode: feature
  review: once
```

| Setting | Choices |
|---|---|
| `workflow.mode` | `feature` (new projects): one builder owns a feature, writes and runs its tests, and resumes its own session on a retry. `separate-tasks`: fresh builders and separate testers ([efficient workflow](docs/efficient-workflow.md)). Omitted: legacy mode, where the coordinator also manages each stop. |
| `workflow.review` | `once` (new projects): one AI review of the whole job after its tasks pass their checks and merge. `ai`: every task is reviewed. `off`: the checks decide. `human`: you review. |

`once`, `off` and `human` need a `workflow.mode`; a project without one reviews
every task. Review findings go back to the same builder up to 3 times without
waking the assigner. Switching between `ai`, `once` and `off` keeps running jobs
going; switching to or from `human` blocks them. See
[review workflows](docs/review-workflows.md).

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
contracts and hotspots after inspecting the project. Under a workflow it cannot
rewrite settings the user already configured. Both a task gate and a `full` gate
are required; in feature mode a builder task with no named gate runs the `full`
gate. Checks must terminate without changing committed files.
[Setup profiles](docs/workspaces-and-environments.md) prepare only required tools
and reuse private npm snapshots or cached Python wheels. Legacy `worktree_setup`
builds private environments; `.venv` and `node_modules` are never
shared. On an empty project, avoid setup commands requiring manifests that the
first worker has yet to create.
## Model policy

| Role / worker preference | Model | Effort |
|---|---|---|
| Coordinator and reviewer | GPT-6.1 Sol; fallback Claude Opus 5.5 | high |
| Worker 1 (default) | GPT-6.1 Sol | high |
| Worker 2 (preferred fallback) | Claude Opus 5.5 | high |
| Worker 3 (easy tasks only) | Claude Sonnet 5 | high |
| Worker 4 (explicit bounded assignment) | Local Qwen 3.8 | local default |

This order is your configured preference, not a measured benchmark. The scheduler
also checks task complexity, runtime capabilities, model access and account quota.
Control roles (coordinator and reviewer) accept only Sol or Opus.
Sonnet requires `complexity: easy`. Explicit Qwen assignments accept standard
and complex tasks on the same complexity scale as Codex and Claude. Assignment
roles can restrict Qwen to backend work and reject frontend tasks. Qwen supports
research and bounded source/test tasks after measured tool confinement passes;
high-risk task kinds still require stronger filesystem isolation. When the user
wants to measure Qwen on one HOTSPOT task anyway, the attached manager records
that decision with `agentkit manager local-trial <job> --task N --evidence-file F`;
Qwen then runs it as a confined bounded writer, the task keeps its HOTSPOT lease,
review and gate, and the approval lapses if the task's spec changes. Decouple
and contract work are never eligible. Complexity
and confinement are separate checks. One local GPU worker runs at a time.
Explicit assignments switch models only through approved fallbacks.

New jobs default to automatic initial selection: Sol, then Opus at high. Once
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

Profile settings support `model`, `effort` and `enabled`. Default control effort
is `high`; exact role assignments can also pin Sol coordinators and reviewers.
Edits do not change an already pinned coordinator.

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
path when installed elsewhere. It uses the configured local model and requires
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
User request -> assigner plans once -> saved task graph
  -> isolated builders -> task checks -> review (per task, once per job, off or human)
  -> integration worktree -> combined full checks -> code accepts the job
```

The supervisor records process ownership, session IDs, generations, heartbeats,
checkpoints and redacted event logs. Closing the initiating agent session does
not close the background supervisor. Use `agentkit service autostart` for optional
Windows logon recovery, or restart it with `agentkit job start` after a machine
restart. Automatic host startup requires that explicit configuration.

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

Quota waits do not use task failure attempts or ask the human to intervene.
Authentication problems and genuinely missing product decisions remain explicit
blockers. Under a workflow, a task that stays stopped after its automatic retries
becomes a problem for the user (next section); in legacy mode it returns to the
coordinator.

With qualified providers, declared gates and permitted model assignments, the
coordinator and supervisor can plan, build, review, integrate and recover without
a human approving each task. The coordinator supplies task complexity metadata;
this is not a calibrated complexity classifier. Live quota-reset reliability
requires observed recovery evidence as well as the automated regression tests.

Under `review: ai`, an independent PASS applies only to the exact clean worker
commit; under `once` and `off`, a task's passing gate approves it. Integration
audits scope, merges that commit in a dedicated checkout, runs the combined full
gate, and rolls back a failed merge. Contract changes freeze a version before
dependent work starts. With a workflow, code accepts the finished job after
another passing combined gate and, under `once`, a passing review of the combined
change. In legacy mode the coordinator accepts it.

**Automated merges target the integration branch**, not protected branches such
as `main` or `master`. This preserves the repository's existing publication boundary.
## When something stops

Automatic retries come first: review findings go back to the builder up to 3
times, and a failed gate or a crash is retried once. A provider's usage limit is
never a problem; the task resumes when the provider is back. A reviewer that
could not review, a builder reporting a tool or setup failure, or an unchanged
resubmission stops the task instead of re-running builders.

What the retries cannot fix becomes a **problem**: code records what happened and
which replies can fix it, with a six-letter code. Tasks that do not depend on the
stopped one keep running and merging. The same setup problem in two tasks of one
job holds that job until you retry one of them, so no tokens are spent on
sessions that would fail the same way.

| Reply | What it does | Tokens |
|---|---|---|
| `retry` | Runs the stopped step again, after you fixed the cause | Only the rerun |
| `accept` | Takes the builder's reviewed commit; its checks must still pass | None |
| `skip` | Cancels the task and every task that depends on it | None |
| `dismiss` | Closes a request for more files; the task keeps its scope | None |
| `hint <words>` | The same builder continues its own session with your words | One builder turn |
| `plan <words>` | The assigner re-plans the job with your words | One planning session |

Answer in the view's **Jobs** tab, from the terminal (`agentkit problem list`,
`agentkit problem act CODE retry`), or by email. Turn email on in
**Settings > Phone (email)** with a separate mailbox and an app password; the
password is encrypted for your Windows user (DPAPI), or read from
`AGENTKIT_PHONE_PASSWORD` elsewhere, and never shown or logged. Each new problem
sends one email. A reply is obeyed only from your own address, with a DKIM or SPF
pass, carrying an open problem's code. `status`, `pause` and `resume` also work.
SMS is designed but not built. See
[when something stops](docs/problems-and-phone.md).

## Token use

Sessions start lean. Codex runs without sub-agents, plugins, apps or the skill
catalog, and Claude without slash commands or dynamic system-prompt sections.
Briefs are capped at 4,000 characters, and plain reads can be batched in one
turn. The assigner wakes only for a new job revision or a decision, and resumes
its own session with a short digest of what changed; past 600,000 input tokens in
its last session it starts fresh from the compact brief instead. Review prompts
shrink to fit the model's context. `budgets.job_tokens` in `.ai/project.yaml`
optionally caps a job's recorded tokens: over it, nothing new launches and the
work so far is kept.

Managed work still costs more tokens than one session. On 2026-10-12, on a small
project with 40 acceptance cases, one solo Sonnet 5.5 session used about 100,000
tokens and AgentKit with `review: once` and Sonnet in every role about 577,000
(5.7 times as many); both passed all 40. An earlier Sol run with per-task review
used 13.7 times the solo tokens and did not finish; see
[the benchmark](docs/evaluations/AGENTKIT_BENCHMARK_2026-10-12.md) and
[the efficiency audit](docs/evaluations/AGENTKIT_EFFICIENCY_AUDIT_2026-10-12.md).

## Operate and inspect

```sh
agentkit view                          # every repository: jobs, problems, settings
agentkit run --watch --max-workers 2   # foreground alternative
agentkit live --follow                 # redacted agent and manager activity
agentkit status
agentkit problem list                  # what waits for you
agentkit problem act CODE retry        # answer a problem
agentkit task retry 7                  # resume a task stopped on a setup problem
agentkit providers status
agentkit providers check
agentkit events --task 7
agentkit why merge-failed 7
agentkit integrate                     # manually process approved merge queue
```

### One view for every repository

`agentkit view` opens one local page for every repository that uses AgentKit:
those where `agentkit init`, `agentkit view` or `agentkit service start` ran, and
their sibling folders that already have `.ai/project.yaml` (task worktrees are
left out). It works from any folder, so after a restart run `agentkit view`, pick
the repository at the top, review its **Settings** tab and press **Start**.

The **Jobs** tab is where you write a job, add screenshots, change a job, and
answer its problems. Other tabs show the manager, agents, sessions and tasks,
usage, reviews and setup.

Settings holds every choice the manager makes: the manager and reviewer model and
effort, the workflow and review choice, each worker assignment's model and effort,
worker slots and maximum workers, a job token budget, which models may launch,
agent windows and turn limit. They save only while that repository is stopped; a
new manager re-pins its unfinished jobs through `agentkit manager repin`, which
waits while a chat manager holds a fresh lease. Provider hold/clear, the local
router's Qwen thinking switch and the phone email settings work at any time. The
phone settings serve every repository. Start lifts `execution_paused` and starts
the background service; Stop ends it, and sessions already running finish on
their own. The list of repositories lives in `~/.agentkit/projects.json` (or
`AGENTKIT_HOME`).

Commit `.ai/project.yaml`, `.ai/tasks.yaml`, `.ai/jobs/*.json` and project skills
as project records. SQLite runtime state, quota windows, process logs and job
screenshots live under `.ai/` and are ignored by Git. Preserve runtime state to
preserve approvals and sessions. A rebuild from Git alone requires re-auditing
recovered branches; it cannot reconstruct missing approval evidence or prove
process ownership.

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

## License

MIT. See [LICENSE](LICENSE).
