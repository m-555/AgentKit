# Model assignments and recovery

Set explicit choices in the target repository's `.ai/project.yaml`. Existing
projects without these entries keep their ranked defaults. A task selects its
named assignment with `model_assignment: backend` or `model_assignment: frontend`.

```yaml
max_workers: 0
model_policy:
  roles:
    coordinator: {profile: sol, model: gpt-6.1-sol, effort: high}
    reviewer: {profile: sol, model: gpt-6.1-sol, effort: high}
  assignments:
    backend:
      profile: opus
      model: claude-opus-5-5
      effort: high
      fallback:
        - {profile: sol, model: gpt-6.1-sol, effort: high, when: [USAGE_LIMIT]}
    frontend: {profile: sol, model: gpt-6.1-sol, effort: high}
```

Exact assignments do not silently substitute a model. The backend fallback above
requires actual usage-limit evidence, an available target provider, confirmed
termination of the old owner, and scope, ancestry and checkpoint checks. It starts
a fresh provider session over preserved work and invalidates earlier review.
After a transfer, the selected continuation remains the owner when Claude recovers.
Authentication errors, crashes and expired quota timestamps do not authorize this
fallback. Existing implicit worker preferences remain available to legacy projects.

`max_workers: 0` means all eligible independent tasks in each finite scheduling
pass. A positive value limits builders; negative values are rejected. This removes
the builder-count cap only. Dependencies, contracts, file leases, capability proof,
provider quotas and the local GPU guard still apply. Coordinator and reviewer
processes are separate from this builder count.

## What actually resumes

The Python supervisor must be running independently of the provider session.
`agentkit job start` starts it; `agentkit run --watch` is the foreground alternative.
Account reset time permits a new availability check. Confirmed availability permits
a launch; a fresh session does not create a fresh account allowance. Five-hour and
weekly windows are tracked separately when reported. Claude checks use a bounded,
tool-free request and can consume a small allowance; Codex reads account metadata.

An unavailable pinned manager records a recovery epoch. Existing authorized work
on healthy accounts may continue; integration and newly dependent work wait for
manager recovery. The returning manager inspects preserved commits, checkpoint
bytes, scope, review and gate evidence, contracts and job revisions. It must audit
and acknowledge the current unchanged epoch before the barrier clears. Failed
work is quarantined rather than accepted. Its provider, model and effort stay pinned.

This resumes an AgentKit CLI coordinator. It does not reopen or send a turn into a
native Codex chat. Native continuation needs a supported host feature configured
and tested separately. Goal mode is documented in [Codex long-running work](https://learn.chatgpt.com/docs/long-running-work);
that documentation does not establish automatic wake after an account quota reset.
After a machine restart, restart the supervisor; OS startup installation is separate.

## External manager ownership

An external manager needs a persistent bridge process, periodic heartbeat and saved
checkpoints. The one-shot `attach` command is not that persistent bridge.

```sh
agentkit manager attach JOB --holder main-chat --pid BRIDGE_PID --ttl 90
agentkit manager heartbeat JOB
agentkit manager checkpoint JOB --file manager-checkpoint.json
agentkit manager status JOB
agentkit manager audit JOB
agentkit manager ack JOB --epoch EPOCH --digest DIGEST --evidence-file audit-notes.md
agentkit manager release JOB
```

The credential is saved under `.ai/runtime/manager-JOB.credential` and is not printed.
Checkpoint fields are `decisions`, `completed`, `remaining`, `tests`, `next_action`
and `blockers`; `next_action` is required. Save useful decisions and failures rather
than raw transcripts. A live or unidentified old bridge still fences a replacement,
even after release. Exit the released bridge before allowing a CLI replacement.
A paused native chat with a live bridge cannot safely be assumed dead from a missing
heartbeat. An expired live holder can authenticate renewal and must complete its
recovery audit. Preserve `.ai/tasks.db` and runtime evidence across restarts.

## Runtime setup and readable source

Run `agentkit --version`, `agentkit doctor`, and `agentkit probe` in the actual target
repository. The version reports stale installed metadata; reinstall with
`uv tool install --editable ./orchestrator` from this checkout. Functional probing
is opt-in, sends real agent requests and must positively prove that authorized
writes work while unauthorized writes are blocked. Static flags alone do not
establish confinement. A changed runtime invalidates earlier capability proof.
For Opus 5.5, Claude CLI must be at least 2.1.280. `AGENTKIT_CLAUDE_CLI` can explicitly
select a compatible binary; discovery otherwise examines available installations.

Reviewers can use `source_list`, `source_read`, `source_search` and `source_diff`
without a shell. These tools read bounded tracked source at the assigned commit,
with credential paths withheld and full source blobs redacted before pagination.
They do not create integration worktrees. A reviewer cannot approve a changing tree.
The supervisor still requires structured review of the exact clean commit and gates.

MCP tool access and command sandbox permissions are separate. A CLI claiming
`workspace-write` can still be constrained by host policy. Verify effective behavior
rather than forcing capability booleans or disabling protections. Read tools have
truthful read-only annotations; `gate_run`, `review_submit`, plan changes and manager
acknowledgement mutate state and must keep their real classifications. If host MCP
approval policy rejects a required operation, report that setup blocker. Explicit
per-tool trust and role allowlists can be configured where the host supports them;
never enable automatic approval for every tool to conceal this limitation.

## Keep delegation efficient

Give each builder an exact write-file list, stable interfaces, exclusions, acceptance
criteria and owned tests. Record transfers before another writer starts. Builders
write tests that exercise changed behavior and run focused checks first. Independent
review reads the supplied commit and evidence, reports actionable defects, and makes
no source edits. Run the combined full suite once the milestone is stable; repeat
only affected checks after fixes unless the change warrants another combined run.
Save concise progress, failures and next actions so recovery does not repeat research.
Use `agentkit live --follow` for redacted runtime activity. It shows AgentKit-launched
processes; native bootstrap tasks need separately labelled reported-progress views.

## Active windows only

On Windows, opt in with `visible_windows: true` in `.ai/project.yaml`. Each
AgentKit-launched process then receives one disposable redacted viewer. It closes
when both its monitor and child have stopped. Quota-stopped and completed windows
do not stay behind; a resumed process receives a new viewer. A terminal database
record with a surviving child remains visible as a liveness risk. Closing a viewer
does not stop its worker. Logs stay under `.ai/runtime/`. A busy database is not
interpreted as process death. The default remains terminal-only for headless
projects and tests. Native bootstrap progress windows need their own completion
signal because AgentKit has no process record for a native sub-agent.

## Native manager as the reviewer

To use an attached native manager as the only reviewer, set
`review_mode: external-manager` in `.ai/project.yaml`. The supervisor waits for that
manager's structured verdict instead of launching an additional review session.
`max_workers: 2` counts the two builders; the native manager makes three sessions.
Attach from the native session so `CODEX_THREAD_ID` is captured in the lease.
The bridge PID must remain live and the heartbeat fresh. A manager recovery audit
must be acknowledged before submitting a verdict after an outage.

```sh
agentkit manager review JOB --task TASK_ID --head EXACT_SHA --verdict PASS --evidence-file review.md
```

The native MCP `review_submit` tool also supports this mode. It checks the same
session identity, independent authorship, clean exact commit, owned scope and gates.
Supervised worker and coordinator sessions cannot submit verdicts in this mode.
Omitting the mode preserves supervised reviewer behavior. This option supplies
review authority; it does not install a native-chat wake bridge.

Windows and WSL are separate runtimes. Authentication inside WSL does not make
the Windows adapter execute Linux Claude. A mixed runtime additionally needs path,
worktree metadata, hook/MCP interpreter, gate execution and child-process ownership
handling. Until those are implemented and tested, do not mark a Windows project
ready from Linux probe results or launch a shell wrapper as a supervised worker.


## Persistent host supervision

`agentkit job start` keeps a code-only guardian around its finite supervisor process.
A crashed supervisor is restarted with bounded backoff (40-300 seconds); an idle
queue is checked again after 20 seconds. `.ai/runtime/supervisor-health.json`
records child PID, retry reason and next delay. Restarts never replace live AI
owners or clear manager recovery barriers, task holds, quota or review evidence.
The guardian reloads installed AgentKit code in each child process.

Host preparation must revalidate task state, definition and generation before
claiming a worker. An assignment changed during preparation is refused without
a paid launch or consumed generation. Availability checks remain per account,
coalesced and provider-confirmed; no early paid Claude cooldown probes.

The guardian covers supervisor crashes and idle queues while its host process
and computer remain running. It does not survive a computer shutdown or a killed
guardian; OS service/startup registration is a separate deployment requirement.
Code can run checks and queue authorized work while an AI manager waits for
quota. New review-dependent integration still needs the selected reviewer.
Native editor recovery remains experimental where only a private bridge exists;
a submitted/started turn does not prove completed or healthy UI recovery.


## Persistent native quota recovery

The CLI scheduler already resumes quota-blocked workers and managers through the
shared recovery ledger. The supervisor also checks explicitly registered native
Codex managers. Register from the active authorizing chat after its manager audit:

```
python -m agentkit.native_session_registration --root PROJECT --job JOB --thread THREAD
```

The command verifies the current native thread, active job revision, external
manager lease, native owner and VS Code process birth. No worker can register it.
Each stopped turn with the exact native `usageLimitExceeded` code creates a new
durable recovery intent. Fresh provider metadata must show every subscription
window available; an expired clock alone is insufficient. A fresh manager lease,
active turn, pending approval/input, changed job, user stop or changed owner fences
delivery. An intervening user turn requires explicit renewed registration after
reading its instructions. Healthy idle turns never generate automatic messages.

Claims are persisted before delivery. Accepted, started and completed receipts
remain distinct. Lost replies or crashes with an unresolved claim require
reconciliation, never a blind retry. A delivered turn that hits quota again starts
its own cycle. A confirmed `serverOverloaded` failure of that exact delivered
turn permits up to three retries after 60/120/240 seconds of backoff (the supervisor
polls at most once per 300 seconds). Each retry needs a new durable claim, fresh
quota availability and unchanged job/thread/owner authority. The budget and exact
failed receipt survive restarts; no model substitution or inference probe is used.
Unknown delivery outcomes, interruptions and other failures still need user action.
Completed work
waits for the next user authorization. Metadata polling is bounded to 300 seconds
per registration and does not consume model inference. Runtime registration JSON
and recovery ledger receipts survive supervisor restarts.

This existing-chat VS Code transport still uses a private interface and remains
experimental until repeated real quota cycles pass. Claude editor wake is
unsupported; Claude CLI continuation uses the supported scheduler. The guardian
survives child failure but not OS shutdown or termination of the guardian itself.
This registration does not install an OS startup service or change UI settings.

A reloaded VS Code view can change its internal client ID without changing the
Code process or chat. Before any delivery, AgentKit can rebind only when the new
client exposes the exact authorized failed quota turn, with no pending input,
and the old targeted client explicitly returns `no-client-found`. Code PID/birth,
thread, turn, job revision and manager authority remain unchanged. Claimed or
delivered intents cannot be rebound. The proof update is journalled and its file
receipt survives a restart; unknown responses and any live old owner fence it.
Refusals now show a fixed reason instead of only `PermissionError`. This remains
an experimental private transport, not a completed real-reset qualification.


## Independent native recovery cadence

Registering the authorized active native chat starts a hidden host service, not
another AI session. It holds an OS singleton lock, ticks the saved registrations
every20s, and preserves their per-session60s active/300s quota metadata deadlines.
Long combined checks and slow unrelated account checks cannot delay its heartbeat.
Native leases retain a minimum300s margin; longer explicitly configured leases
are preserved. Actual stopped-quota wake still requires expiry and all existing
identity, availability, revision and intent fences. Database contention retries
without cancelling a registration or delivering a second turn. Unknown outcomes
remain fenced. The supervisor restarts a stale/dead host recovery service when
an enabled registration exists. Runtime native-recovery-health.json reports its
identity and cadence independently of the development supervisor. The native
VS Code bridge remains private and cannot be called proven until a real quota
reset yields an observed completed continuation turn.
