# Separate implementation and testing

This optional workflow uses small file-owned tasks and a fresh provider session
for each task and retry. It applies to new projects without making a particular
chat, provider, model or reviewer mandatory. Existing projects retain their
current mode until their manager explicitly enables and replans this workflow.

## Choose a team per project

An example human-review team has one planner/manager, two backend builders,
two backend testers, two frontend builders and two frontend testers: nine slots.
Each pool can have two or three slots. AI review adds an independent reviewer.
These are configured slots, not a reason to launch agents without useful work.
Provider quota, isolation certification, dependencies and file leases still
limit actual concurrency. All sessions on an account share its allowance.

The user chooses manager, reviewer, builder and tester models independently
through model_policy.roles and model_policy.assignments. Claude Code can manage
and review another project. Codex is not the permanent manager. Each
project chooses its own model policy; no project-specific task graph belongs in
AgentKit source or its reusable templates.

The browser dashboard exports YAML without changing the project. Optional local
operator controls can apply a profile to an idle project; neither route launches
models. Apply a profile before submitting a job, then validate its task graph
before activation. Native external-manager identity and same-chat wake
are adapter-specific opt-ins; they are not promised for every chosen manager.
Use the existing supervised coordinator/reviewer mode when not using a supported
native external-manager bridge. A manager/reviewer must be independent of the
implementation it approves; a builder cannot approve its own task.

## Task contract

Each assignment contains one outcome, exact read/write files, a frozen interface,
dependencies, acceptance checks and one short checkpoint. Defaults: at most
three write files, six read files, 1,200 assignment characters (title, description and acceptance combined) and 16,000
job-context characters. Oversized assignments require splitting or replanning;
the system must not silently omit authority or acceptance requirements.

- backend-builder and frontend-builder implement source only. They do not write
  or run tests and do not review other tasks. Their gate is source/style checks.
- backend-tester and frontend-tester own test files only, use TEST_ONLY and name
  their implementation dependencies. They write and run focused behavioral
  tests; failures return to the manager for a separate source repair assignment.
- The planner/manager defines tasks and resolves meaningful blockers. In AI mode
  the reviewer approves exact worker commits, then the coordinator checks combined
  acceptance evidence. In human mode code stages checked commits and the user
  tests the combined feature. The planner waits between decision events rather
  than continuously rereading files or duplicating a passing full suite.

Builders can be reviewed and merged after their source gate so dependent testers
receive their code. This is intermediate source acceptance, not final feature
acceptance. The complete feature remains unaccepted until independent tests and
the combined integration gate pass. Human mode uses explicit mechanical staging
records and final human acceptance; AI mode retains independent review. Neither
mode bypasses scope, identity, contract, recovery or protected-branch checks.

Fresh sessions recover from a concise checkpoint and preserved worktree, never
from another task's conversation. Existing process ownership must end before a
replacement runs. Approved quota fallback is a separate project policy and is
not permission to substitute a model or bypass limits. Avoid model-based pings
for healthy agents; provider checks are coalesced per account.

## Live session view

Run `python -m agentkit.dashboard --root PROJECT --port 8765 --open` using the
project's AgentKit environment. It binds localhost and is read-only. For a WSL
authoritative runtime, run the server in WSL and open its forwarded localhost
URL from Windows; do not open the active SQLite database with a Windows writer.
Cards show role, numeric task, task overview, assigned files, model provenance,
activity, liveness and token counters. Finished sessions are hidden by default.
Raw prompts, reasoning, credentials and tool arguments are not displayed.

Counters are provider-reported: input, output, explicit thinking, cache reads and
cache creation are separate. Claude input excludes cache reads/creation; Codex
input includes cached input. Thinking may be included in output, so do not add
it to output again. Missing counters are unknown, not zero. Large initial logs
are read as bounded partial tails; partial data is labelled. Subsequent refreshes
read only appended bytes. Usage numbers are not subscription quota percentages
or a bill. External native sessions without provider events have unknown counts.

## Configuration

```yaml
max_workers: 8
workflow:
  mode: separate-tasks
  max_write_files: 3
  max_read_files: 6
  max_task_chars: 1200
  max_context_chars: 16000
execution_limits:
  max_turns: 16
  max_tool_calls: 32
  max_runtime_seconds: 900
  max_output_tokens: 20000
model_policy:
  roles:
    coordinator: {profile: opus, model: claude-opus-5-5, effort: high}
    reviewer: {profile: sol, model: gpt-6.1-sol, effort: high}
  assignments:
    backend-builder: {profile: opus, model: claude-opus-5-5, effort: high}
    backend-tester: {profile: opus, model: claude-opus-5-5, effort: high}
    frontend-builder: {profile: sol, model: gpt-6.1-sol, effort: high}
    frontend-tester: {profile: sol, model: gpt-6.1-sol, effort: high}
```

Add workflow.review: human for nine slots with user acceptance, or ai for
ten slots including the independent reviewer. Matching task roles use their
named model assignments automatically; explicit task model pins override those defaults. A workflow.worker_slots enforces per-role scheduling quotas;
the task graph and maximum worker count determine actual available parallelism.
Measure completed-task tokens/time before claiming this workflow saves allowance.


## Local Qwen for future projects

The local worker picker is optional and does not activate a model. Configure
the model ID for each project's router. Existing local-router tests cover model
lifecycle, proxy, thinking and extension sessions; those checks verify the router,
not the model's ability to write useful tests.

The current local-opencode AgentKit adapter only allows explicitly easy RESEARCH
and reports unattended write isolation as unproven. Do not relabel those
capabilities or assign TEST_ONLY until a measured isolated-write qualification
passes. Never use Qwen as manager or final reviewer under the present policy.

A future opt-in quality exercise can first request test source as an artifact
through a read-only research task. Give it one dependency-free function and
one contract, with a small output budget and no project files. A separate trusted
tester compiles/runs that artifact in a disposable worktree, verifies meaningful
assertions and checks that seeded behavioral bugs fail. Include exception paths,
timeouts and malformed output; compare time and tokens against a small baseline.
Only after quality and isolation qualification should future projects enable
Qwen test-file writing. The configurable local model ID must match their router.

## Runtime bounds and allowance reporting

Separate-task workers default to 16 provider-reported turns, 32 tool calls,
15 minutes and 20,000 reported output tokens. Explicit execution_limits overrides
must be positive integers. Claude also receives its native --max-turns flag.
Some usage arrives only at completion, so output limits cannot guarantee stopping
before every token is spent. Thinking included in output is never added twice.
Unknown counters remain unknown. Controls keep their chosen effort; explicit
medium, high, xhigh and max settings are supported without changing an existing
job's persisted manager pin. The view exports the choice or applies it with operator controls to an idle project.

At a bound, preserve work and block for manager inspection. Failed strict-mode
tasks are not automatically repeated. Reuse a passing gate only at the same
clean commit. Builders run only their source gate; testers own test-only tasks.
execution_paused: true stops new work, account probes and automatic integration.
Pausing does not kill an already-running owner; inspect it before handoff.

Account allowance is displayed only from provider window metadata. Cached tokens
are repeated cache reads across turns, not unique files or allowance percentages.
Stale data, missing percentages and a passed reset deadline are labelled explicitly.
A healthy Claude account is not pinged using paid inference. A due quota recovery
check may consume a bounded request; a clock alone never proves recovery.

Agent turns are the provider's reported steps within a session, not task counts
or process numbers. Claude num_turns is shown; Codex counts remain unknown when
the stream does not expose equivalent model-loop counts. Run # is the database
process number; Task # and the provider session UUID are separate identifiers.

## Manager design

The initial v3 core separated task planning, deterministic scheduling/recovery,
review and integration. Autonomous jobs later added a provider-pinned coordinator
that inspects the repository and maintains the task graph. The manager should
decide scope, interfaces, owners and next steps, then wait for completion events.
Review completed diffs and independent evidence once. Mechanical probes, leases,
process liveness and unchanged-commit gates belong to code rather than model loops.
More worker slots are useful only when distinct tasks can run without contention.

Live qualification is separate: Windows/WSL mixed-worker transport and
VS Code quota-reset wake must pass their actual runtime checks before a project
relies on them.
Unit and protocol tests do not establish those live guarantees.

Strict workers run ongoing L5 lease audits in their independent host monitor,
with a final exit/merge audit. Worker-side PostToolUse skips duplicate L5 only
when this mode is configured; virtual readonly sandbox mounts are not interpreted
as real changed files. Real out-of-scope writes preserve work and block for manager
inspection. Pre-write, staged-commit and integration checks still enforce scope.

Strict builders and testers cannot create nested native Claude/Codex agents.
Use AgentKit-owned tasks and model policies for each session instead. Validate
the launch text and the forthcoming brief together before a model request.
Completed and cancelled historical scopes are not new strict-mode assignments.

## Code handles routine handoffs

Planner, assigner and manager are responsibilities of one user-facing coordinator,
not three required sessions. Human review and AI review are the two implemented review choices. A separate
manager workflow is unnecessary. See [review-workflows.md](review-workflows.md)
for configuration, job submission, user decisions and tested boundaries.

The planner declares the bounded builder/tester graph once. A tester names a
same-area source builder in the same job and includes that builder's exact source
files in its read scope. Definition and activation reject invalid pairings. Code
promotes dependencies after integration and builds a tester brief containing the
approved builder commit, source paths, behavior criteria and passing-gate marker.
It does not copy the builder conversation, paste whole repositories, invent test
paths or call a model to rewrite the handoff. Tests remain in the tester's exact
owned files; source inputs are available in its private checkout.

Before launching a tester, code verifies the prerequisite is DONE, its latest
review approves that commit, its task gate passed, and the checkout contains the
approved source version (including deletions and file modes). Missing/stale inputs
refuse launch before generation/leases/model requests. The assembled brief remains
subject to the existing context limit; excess criteria require replanning.

Builders and testers finish into the existing approval/integration path. Test
failures and ambiguous requirements return to the planner for a bounded repair;
they never authorize an automatic source fix by the tester. Scheduling, packet
construction, gate execution and ownership checks are code responsibilities;
interpreting user intent, task boundaries and product decisions remain planner
responsibilities. No new sessions are needed to perform the handoff.

## Host commits and source formatting

WSL workers use MCP `task_commit`, never Linux Git against the host repository.
Commit messages support 1-2000 characters, including normal multiline bodies.
Commit before calling `gate_run`, which requires a clean recorded commit.
Set `source_line_endings: crlf` (or `lf`) in the project configuration to normalize
only changed, audited UTF-8 source files on the host before staging. The default
preserves bytes. Binary files and unchanged files are untouched; escaped paths,
links, out-of-scope diffs and stale generations cannot acquire formatting authority.
Workers should not spend model turns finding shell formatting alternatives.
Concrete worker or manager blockers survive dependency refresh; only explicit
recovery or verified provider-reset handling can release their hold. Cancelled
drafts remain quarantined with byte and violation evidence and cannot integrate.

Role pool limits are applied while selecting the compatible worker group, before
the overall worker slots are consumed. A second backend task waiting for its pool
does not prevent a ready frontend task filling a free slot. Ownership and dependency
checks still govern parallel execution.
