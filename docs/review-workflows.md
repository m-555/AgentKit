# Two review workflows

One user-facing coordinator plans, assigns and manages a job. It waits between
new requests, meaningful blockers and completed work. There is no third manager
workflow. The coordinator model is chosen per project and remains pinned for the
job; the framework does not require Codex to manage every project.

| Choice | Task staging | Final feature acceptance |
| --- | --- | --- |
| human: lower expected AI usage | Code verifies scope, task checks, identity and merge preconditions | User tests the combined preview and approves its exact version |
| ai: higher expected AI usage | Independent AI reviewer approves exact worker commits | Coordinator checks combined acceptance criteria |

These labels describe expected relative usage, not a promised subscription
percentage. Actual provider counters remain separate. Thinking is already inside
Claude output; cached reads accumulate across turns. Unknown values remain unknown.

## Configure before creating jobs

Use the dashboard Team setup to choose models, effort and 2-3 slots in each pool.
Human review uses one planner plus worker slots. AI review adds a reviewer.
Separate-task roles automatically use the matching named model assignment;
explicit task pins override those defaults. Legacy projects retain their routing
and review behavior until explicitly opting into this workflow.

A minimal profile (preserves existing model policy and execution pause):

```yaml
workflow:
  mode: separate-tasks
  review: human # or ai
  worker_slots:
    backend-builder: 2
    backend-tester: 2
    frontend-builder: 2
    frontend-tester: 2
max_workers: 8
```

Apply without starting any AI session:

```text
agentkit --path PROJECT workflow apply --file profile.yaml
agentkit --path PROJECT workflow show
```

Apply refuses live/uncertain session owners and unfinished jobs. It preserves
existing execution_paused, gates and unknown project settings. Choose a profile
before creating a job; changing roles during an existing job is not a shortcut
around its saved coordinator pin or recovery barriers.

## Convert a never-started plan

An operator can explicitly replace the team policy for one existing PLANNING
job that has never launched a worker or control session:

```text
agentkit --path PROJECT workflow apply --file profile.yaml --replan-unstarted JOB
```

This refuses previous process history, started task generations, outstanding
leases, uncertain owners, recovery holds and other unfinished jobs. It saves a
new durable request revision, invalidates the old plan approval and forces
execution paused. Rebuild and validate the exact-file task graph before planning
or activation. Existing task history is retained; this is not a way to transfer
live work or bypass launch qualification. Ordinary profile apply still refuses
unfinished jobs.

## Activity tabs

Working shows owned live sessions and active external-manager registrations.
Waiting shows unowned tasks, with reasons such as project pause, inactive job,
unmet prerequisites, quota, pending review or pending integration. A ready task
is awaiting scheduler checks; it is not evidence that a model is working.

Needs attention contains uncertain ownership, terminal-but-live processes and
tasks whose recorded active state has no live owner. History separates completed
sessions/tasks, failed or crashed sessions, stale registrations, cancellations
and recorded transfers. Transfer labels name the target provider/model and
require a matching recorded handoff; they do not imply the replacement is alive.
Stale means missing heartbeat evidence, not successful completion. Counts and
keyboard-accessible tabs persist across local refreshes. Only the selected
category is rendered, with an additional history filter. Each tab shows at most
12 records per page and retains its page when switching tabs or refreshing.

## User job file

Copy plugins/agentkit/templates/job-request.yaml outside the AgentKit source into
the project or another user-selected location. The format is:

```yaml
id: retry-message
coordinator: auto # or codex / claude-code
request: |
  Improve the retry message while preserving the existing retry behavior.
acceptance:
  - Existing retry limits and successful results stay unchanged.
  - A timeout produces a clear message.
```

```text
agentkit --path PROJECT workflow import-job --file job-request.yaml
```

Import only saves durable intent and a PLANNING job. It does not start supervision,
launch workers or run provider checks. The request plus acceptance is bounded to
12,000 characters. Existing job create/update commands still work. Jobs and runtime
records belong to the target project; reusable templates contain no project tasks.
Start supervision only after that project's actual runtime guards and gates pass.

## Code handles the routine path

The planner creates bounded source-builder and TEST_ONLY tester tasks once, with
exact files, acceptance criteria and dependencies. Human mode requires independent
tester coverage of every source builder. The scheduler respects per-role slots,
leases, provider availability, guards and current plan/recovery authority.

After implementation, code assembles a tester packet: approved commit, source
paths, required behaviors and gate evidence. It verifies the correct source version
is in the tester checkout before launching a fresh session. It copies no builder
conversation and needs no extra planning call to rewrite a test prompt. Builders
write source only; testers write/run focused tests only. Defects return to the
planner as events for bounded repair assignments, never source edits by testers.

Human mode records automation:human-staging. This is mechanical approval for the
integration preview, not an AI code review or human product approval. Audit, clean
commit, identity, frozen-contract, combined-check and recovery requirements remain.
Main/master is never automatically published. AI mode refuses mechanical records
as substitutes for independent reviews.

Completed human-mode tasks lead to AWAITING_USER. No AI reviewer or final acceptance
planner session is needed for this transition. Unchanged blockers do not relaunch a
strict-mode planner every timer tick. Failed strict tasks, new user revisions,
changed task state and recovery evidence create new decision events. A confirmed
provider recovery may resume a provider-failed planner; elapsed reset time alone
is not evidence that allowance is available.

Passing checks can be reused within the same private checkout at the same clean
commit, commands, setup, dependency-lock fingerprint and installed manifests.
Dirty or changed inputs invalidate reuse. Legacy projects continue to rerun checks.
Set workflow.cache_checks: false to rerun in the separated workflow too. Run the
existing gate command explicitly after external service/environment changes; no
cache claims to certify a live external service or arbitrary ignored file changes.

## User review in the view or CLI

The view stays read-only unless explicitly started with operator controls:

```text
python -m agentkit.dashboard --root PROJECT --port 8765 --operator-controls --open
```

Use the project's authoritative runtime OS/environment. For a Linux-owned runtime,
run the server/CLI in WSL; do not open its SQLite database with a Windows writer.
Mixed Windows-host/WSL-worker transport still needs separate qualification.

Preview cards show user requests, acceptance criteria, combined checks, exact commit
and the private checkout to test. Launch/test the project's UI from that checkout;
AgentKit does not invent or execute an application startup command. Check the
requirements, enter test notes and click Approve tested preview. A changed preview
requires explicitly loading and testing the new version. Typed notes survive
polling; approval cannot silently move to a newer commit.

Request changes appends a durable user request, increments its revision and returns
the job to PLANNING. The planner receives that event when supervision resumes;
the HTTP action itself never starts an AI session. Decisions bind job revision,
preview commit, task proofs, review policy and verification digest. Worker/control
sessions cannot submit operator decisions. Server actions require local Host/Origin,
a current process token, bounded JSON, and an exact supported route.

CLI equivalent:

```text
agentkit --path PROJECT workflow preview JOB
agentkit --path PROJECT workflow decide JOB --revision REV --head SHA --digest DIGEST --verdict PASS --evidence-file notes.md
```

Use CHANGES instead of PASS for feedback. PASS is available only for human-mode jobs.
A verified acceptance completes the staged job; it does not merge into main, deploy,
or publish. Provider choice/effort changes apply to future sessions, not an already
running model. Local Qwen remains optional for qualified future research tasks;
write/test execution is not enabled merely by choosing it in a profile.

## Verification and limits

The automated suite uses real disposable Git repositories, gates and local HTTP
requests with simulated providers. It covers staging, independent testers, exact
human decisions, stale/dirty previews, durable feedback, protected main, profile
application, automatic role routing, worker caps and planner event deduplication.
It makes no claim of successful VS Code quota-reset wake or real provider efficiency
benchmarks. Native same-chat wake remains diagnostic. A project stays paused until its own
transport, write guards and required checks are qualified.
