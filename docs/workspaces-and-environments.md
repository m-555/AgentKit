# Workspaces and dependency preparation

AgentKit keeps source checkouts, dependency preparation and AI conversations as
separate lifetimes. A fresh conversation does not require a fresh install.
The host prepares dependencies before starting a worker; setup makes no model
calls. Builders and testers retain their separate file scopes and task checks.

## Storage

New projects use a sibling directory named `wt-<repository-directory>`.
Set `worktree_root` in the target project's `.ai/project.yaml` to override it:

```yaml
worktree_root: E:/projects/wt-example
dependency_cache_root: E:/projects/.agentkit-cache
```

Worktree storage must be an absolute external directory, outside the source
checkout and its ancestors. Task folders use stable spec IDs and hashes.
The integration checkout lives in that same directory under `integration`.
Recorded paths take precedence: changing configuration does not silently move
existing work. Retries and provider continuations preserve the task checkout.

## Profiles

`init` selects setup profiles from detected stacks. Existing configurations
without profiles keep their legacy `worktree_setup` behavior. Configure exact
commands for your project; AgentKit does not infer arbitrary project dependencies.
Gate mapping takes precedence over role mapping. Every configured gate must be
covered by the selected profile's declared tools.

```yaml
environment_profiles:
  static:
    setup: []
    requires: []
    tools: []
    reserve_bytes: 0
  javascript:
    strategy: snapshot-copy
    setup: ["npm ci"]
    requires: ["node_modules/.package-lock.json"]
    tools: [javascript]
    reserve_bytes: 2147483648
  python:
    strategy: python-wheels
    setup:
      - "python -m venv venv"
      - '"venv/Scripts/python.exe" -m pip install -r requirements-dev.txt'
    requires: ["venv/Scripts/python.exe"]
    tools: [python-packages]
    reserve_bytes: 1073741824
  combined:
    components: [python, javascript]
    setup: []
    requires: ["venv/Scripts/python.exe", "node_modules/.package-lock.json"]
    tools: [python-packages, javascript]
    reserve_bytes: 0
environment_gates:
  static: static
  frontend: javascript
  backend: python
  full: combined
```

Profiles may also declare `checks`, a list of bounded, side-effect-free host
commands such as dependency imports. They run after setup and before each warm
reuse; missing dependencies hold the environment before an AI worker launches.
Setup success or existing directories alone do not prove readiness. Checks do
not install packages or invoke models; repair remains an explicit host action.

The example Python commands are for Windows host preparation. Match paths and
runtime to your project. WSL execution does not make a Windows virtualenv a
Linux environment. Snapshots and wheels include platform/runtime in their keys.

Strategies:

- `private-cache`: run the declared setup with shared package download caches.
- `snapshot-copy`: opt-in npm preparation reuse as a private copy. Requires one
  simple `npm ci` command, a lockfile and no workspace install/prepare scripts.
  Only recognized workspace links are retained and rebound into the new checkout.
  Arbitrary dependency links are refused. Do not use for path-sensitive packages;
  select `private-cache` for those projects.
- `python-wheels`: cache pinned wheels from the first prepared environment.
  Later worktrees create a virtualenv at their final location and install those
  wheels offline. Editable, local and direct-URL frozen dependencies are refused;
  use `private-cache` when that restriction does not fit the project.

Installed environments are never linked to another writable checkout. Copies
still consume disk space, but require less setup work; optional retirement bounds
that space. OS-enforced shared read-only environments are not implemented.

Readiness keys include dependency manifests, nested tracked package manifests,
lockfiles, profile/component configuration, platform and interpreter/tool versions.
Receipts also observe required outputs and installed package records. They are
readiness evidence, not a complete hash of every installed dependency byte.

## Setup failures and capacity

`min_free_bytes` defaults to 256 MiB. `reserve_bytes` is the estimated preparation
space, not a measured upper bound. Shared host reservations prevent simultaneous
preparations from spending the same free space. Actual disk exhaustion can still
occur; it becomes a durable setup hold rather than another model attempt.

Failed setup is held until inputs/configuration change or an operator explicitly
requests repair. Disk-full holds can retry after observed capacity recovers.
Receipts under `.ai/runtime/environments` record key, profile, duration, attempts,
strategy, readiness/blocker, reserved/free bytes and `ai_calls: 0`.
The dashboard's Worktrees panel displays this evidence on explicit refresh.
It does not ask an LLM to diagnose dependency failures.

```powershell
agentkit --path E:/projects/example workspace setup-repair --task 12
agentkit --path E:/projects/example workspace setup-prepare --task 12
```

Repair permits another host attempt; it does not resume a paused feature job.

## Explicit moves and recovery

Stop workers and pause execution before changing workspace paths. Back up
`.ai/`, including its database and workspace manifest, together with Git data.
A move requires stopped ownership, matching Git registration and a free direct
child of the configured storage folder.

```powershell
agentkit --path E:/projects/example workspace move --task 12 --to E:/projects/wt-example/task-folder
agentkit --path E:/projects/example workspace recover
```

Dirty work normally blocks a move. `--preserve-dirty` explicitly permits the
operator-authorized move, verifying staged, unstaged and untracked source before
and after it. Git performs the move; the registry and current task path are
updated through a recoverable journal. Historical launch records keep the paths
at which those launches occurred. Pending migration journals block new launches.

## Retirement and wake controls

`worktree_environment_cleanup: true` removes reproducible environments after
successful integration and stopped ownership, keeping checkout source and branch.
`worktree_archive_completed: true` instead retires the whole clean integrated
checkout. It preserves branches, commits, task records and archive evidence.
Both settings are opt-in. Dirty or unintegrated source is always preserved.
An operator can archive one eligible task with `workspace archive --task 12`.

`execution_paused: true` prevents scheduling and native wake submission.
Cancel a specific native chat watcher durably with:

```powershell
agentkit --path E:/projects/example workspace wake-cancel --thread THREAD_ID --reason "user paused work"
```

Submission acceptance is not proof of a completed chat turn or quota recovery.
Cancellation is checked before wake submission. An already submitted request
cannot be recalled, and a stopped watcher cannot wake anything.

## Worker startup contract

For separate-task workflows, host code verifies every declared input file and
creates only the assigned output parent directories before dependency setup and
before claiming a generation. Missing or escaping inputs block launch for host
or manager repair. Workers never install packages or diagnose setup; they report
BLOCKED and stop if an unexpected preparation problem appears during their task.
Runtime, guards and MCP configuration are also built by the host before spawning.

### Noninteractive Codex MCP lifecycle

Worker launches explicitly approve only AgentKit's scoped brief, ownership,
checkpoint, host commit, declared gate and task-status tools. Unknown server
mutations still require approval and therefore cannot run under `never`.
Review sessions can submit review verdicts, but cannot commit worker files.
This fixes a preserved frontend task whose MCP commit was rejected before the
host could execute it. The workspace sandbox, exact hook trust, generation,
lease and commit audits remain enabled.

The installed CLI's `config/read` was checked without an AI inference call;
its effective task_commit and gate_run approval modes were `approve`.
Reference: https://learn.chatgpt.com/docs/extend/mcp?surface=cli


### Host completion and missing deliverables

An authenticated native manager can finish exact preserved failed-worker bytes
through host_completion, without launching another model. It requires a stopped
owner, unchanged checkpoint generation/HEAD/dirty digest, exact branch and scope,
prepared dependencies, passing static gates, the ordinary Git hook and the declared
task gate. This produces REVIEW, never approval or DONE; independent review and
integration checks follow. Rejected commits preserve bytes and a new checkpoint.

A passing baseline build no longer substitutes for missing expected_write files.
Code checks declared outputs before worker completion or a PASS review. A blocked
L3/L4 attempt may be acknowledged against a clean exact commit by the native
manager; the violation stays in history. Post-write scope breaches cannot be waived.


A stopped task with no source work may use host task_refresh_empty to advance
its existing branch to accepted integration inputs. It keeps the checkout and
private environment; no model, reinstall or new worktree is needed. Dirty or
committed worker changes and pending quota proofs refuse this operation. The
Git/SQLite journal preserves old checkpoints and recovers an interrupted host
update. It stays held until the manager explicitly replans it. Committed tester
work instead uses test_refresh with an accepted source correction; assertions
stay byte-identical and need fresh exact-commit review and full integration.
