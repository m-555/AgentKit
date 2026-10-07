# Unified recovery and wake policy

Accepted direction: one recovery policy for every worker and planner/manager,
independent of provider or whether the session uses a CLI or editor extension.
This design is generic AgentKit infrastructure. Project tasks and model choices
stay in the target project. This document distinguishes the target design from
currently verified host adapters; it is not a claim that every host wakes today.

## One host-owned lifecycle

1. Register session identity, role, provider/account, model/effort, host transport,
   exact assignment, worktree/branch, generation and process birth identity.
2. Host preflight prepares declared inputs, output parents, private dependencies,
   MCP discovery/approval and measured write confinement before model inference.
   Setup failure is a manager blocker; a worker never diagnoses its environment.
3. Code observes events and saves mechanical checkpoints. Classify task failure,
   crash, authentication, transient capacity and account allowance separately.
4. On a quota stop, retain ownership and bytes, stop/prove termination, persist
   the recovery intent and pause only sessions sharing the exhausted account.
   Prefer non-inference account metadata. Codex uses account/rateLimits/read. Claude
   CLI currently has no registered metadata-only check: its existing bounded, tool-free
   availability request may consume allowance after a cooldown. Never repeatedly
   probe healthy Claude sessions; expose this adapter limitation and cost honestly.
5. Provider-confirmed availability permits a wake attempt. A reset clock by itself
   does not. Weekly exhaustion continues to block after a five-hour reset.
6. Before waking, code checks current user pause/cancel intent, task revision,
   source/generation/dirty digest, old-owner termination and the selected adapter.
   For a manager, code captures a mechanical audit; after waking, the manager
   acknowledges its recovery epoch before development or integration resumes.
7. Use one idempotent delivery claim. Record requested, accepted, started and
   completed separately. An ambiguous delivery never creates a duplicate owner.
8. A resumed worker gets a compact current assignment/checkpoint. A manager gets
   the current event summary and resolves decisions; neither reloads a whole old
   transcript merely to keep the session warm.

A host-owned watcher/supervisor must remain alive when every AI session is quota
blocked. Its timers, ledger writes and ownership checks have no inference cost; provider
availability checks follow the adapter limitation described above. OS startup/restart and editor closure
are separate health conditions and must be visible, never silently assumed ready.

## Provider policy and host transport are separate

| Provider policy | Account allowance | Other failures |
|---|---|---|
| Claude / Codex subscription or metered account | Shared real provider windows; all blocking windows must recover | Authentication, network, crashes and task failures are separate |
| Local Qwen or another unmetered local model | No five-hour or weekly quota window; never invent one | Connection, GPU capacity, context size, process crash and configured task budgets still apply |

A local endpoint returning busy/429 needs capacity backoff, not a subscription
reset. Unmetered is not a license to disable task ownership, confinement or gates.
A project must explicitly opt in to local-worker roles after qualification; availability is not proof that a model can write reliable tests.

| Host | Current status | Required common behavior |
|---|---|---|
| AgentKit-supervised Claude CLI | Existing quota/checkpoint/resume path; real reset behavior must be evidenced per runtime | Host relaunch after availability, stopped-owner checks and compact recovery packet |
| AgentKit-supervised Codex CLI | Existing quota/checkpoint/resume path; scoped lifecycle MCP approvals now explicit | Same lifecycle and observable evidence as Claude; no sandbox widening |
| Codex VS Code native chat | Experimental private IPC, not a supported production guarantee | One-shot current-chat wake; unsupported/failed must be visible as needs user action |
| Claude editor/native chat | No generic registered wake adapter currently verified | Do not claim parity; require a supported adapter or report manual resume required |

The policy and dashboard vocabulary must be the same. Transport-specific support
may differ. Selecting a provider is not proof that its editor extension can be
woken. Capability evidence must distinguish unsupported, unverified and verified.

## Roles and user intent

Planner/manager is one role, chosen per project; Claude, Codex or a qualified local
model can fill it where the host/runtime supports the required capabilities.
Workers and managers follow the same recovery machine. Only managers need the
additional audit/ack barrier. Review remains the project's human/AI choice.

A user pause or stop revokes wake intent immediately. An idle chat is not a reason
to restart development while the user is reading a plan. A later user instruction
supersedes an old one-shot authorization. Explicit resume reauthorizes current
work; it does not allow an old watcher to replay stale instructions.

Fallback to another provider is project-authorized, never automatic reverse
fallback. Transfer only after termination, exact checkpoint/digest validation,
exclusive ownership and a fresh compact assignment. A recovered primary provider
must not reopen work already owned by its replacement.

## Evidence and delivery work still needed

Use host adapters behind this common contract: availability policy, arm/cancel,
probe health, request wake, observe start/completion and reconcile unknown outcome.
Persist support status, reason, next check/reset time, owner, attempts, checkpoint
and last evidence time. Show waiting-for-quota, waiting-for-host, paused-by-user,
needs-user-resume and recovered as different dashboard states.

On 2026-10-04 the native Codex diagnostic observed actual exhaustion and provider
recovery, but made zero submissions because VS Code retained systemError after a
failed turn. AgentKit now permits that unchanged terminal state only for a
quota-only wake with actual exhaustion and freshly confirmed recovery. Pause,
pending-input, owner/peer changes and one-shot delivery checks remain. Provider-free
regression tests pass; the corrected real next-reset delivery remains unverified.

Local loopback adapters now explicitly declare unmetered allowance: health uses
bounded seconds of backoff, subscription windows are discarded, quota pauses are
refused and the allowance view shows local health instead of a reset clock.

Implemented: central recovery_sessions, recovery_intents and recovery_journal tables;
transactional delivery claims, shared availability policy/capability registry, guarded
CLI launch/monitor receipts, native diagnostic receipts, operator status/cancel CLI
and dashboard recovery states. Provider-specific external identity replaces Codex-only
review and host-recovery checks. Deterministic qualification uses no inference.
Real reset qualification remains necessary for each runtime/host; unsupported editor
interfaces remain manual action, rather than being reported as working.

## Durable contract for implementation

The generic session registry now supplements existing process/manager authority; it
does not replace their credentials, independent review, confinement or merge gates. Bind
credentials to registered provider, account, host, role and session reference;
a Claude manager must pass exactly the same authority and recovery audit as a
Codex manager. Provider subscription policy is account-specific, not inferred
from the model name: a cloud Qwen endpoint can be metered, while an explicitly
configured local Qwen endpoint is unmetered. Never infer quota from token counts.

Persist each recovery intent with these fields:

- Identity: intent ID, project/job/task, role, provider/account, host adapter,
  session reference, old monitor/child birth identities and worktree/branch.
- Authority: user authorization revision, task generation, resume strategy,
  fallback permission, checkpoint/head/dirty digest and cancellation reason.
- Observation: failure class, quota/health evidence time, blocking windows,
  earliest next check, adapter support/health and recovery audit epoch.
- Delivery: attempt ID, claim owner/lease, requested/accepted/started/completed
  timestamps, reconciled outcome and last failure. Acceptance is not completion.

Store these in the host database with transactional unique claims, not in a
model transcript. Journal each transition for the dashboard and crash recovery.

The shared states are STOPPED -> WAITING_AVAILABILITY -> READY_TO_WAKE -> CLAIMED
-> DELIVERY_ACCEPTED -> TURN_STARTED -> RECOVERED. Any state may become CANCELLED
on revoked authorization, or NEEDS_USER_ACTION for unsupported transport or
unresolved ownership. A manager's READY_TO_WAKE requires a mechanical audit packet; its awakened
turn must acknowledge the audit before assigning new work or merging. Requiring
the sleeping manager's acknowledgement before delivery would deadlock recovery.
A wall-clock reset only schedules an availability check. It never marks a turn
recovered. Delivery timeouts go to RECONCILING; they do not trigger a second
submission until the adapter proves the first did not start.

Each adapter implements capability(), health(), deliver(intent, attempt_id) and
observe(attempt_id). CLI adapters may start a fresh bounded process from the
checkpoint. Editor adapters target the existing registered chat only where a
supported API or qualified experimental bridge exists. The adapter may report
UNSUPPORTED; identical policy cannot manufacture an unavailable extension API.
Host startup reconciles pending intents and surviving processes before launch.
The supervisor and watcher use code only and remain alive during AI outages.

Local unmetered recovery ignores five-hour/weekly fields entirely. A busy local
GPU uses bounded health/capacity backoff; missing authentication or unreachable
endpoints have their own statuses. Provider outages do not consume task attempts.
Configured per-task turn/token/time ceilings are separate from provider allowance.

## Qualification matrix

Run deterministic tests for both worker and manager roles through a fake CLI and
fake editor adapter, for both subscription and unmetered account policies:

- Five-hour recovery with weekly allowance exhausted stays blocked.
- Fresh available evidence resumes once; restart cannot duplicate the delivery.
- A lost acknowledgement reconciles actual start before another attempt.
- User pause/cancel or newer instructions revoke the old intent.
- Live, ambiguous or replaced process identity prevents competing ownership.
- Fallback keeps source bytes and transfers once; primary recovery does not reclaim.
- Manager wake receives an audit packet; further development waits for ack.
  Worker recovery does not impersonate a manager.
- Local Qwen never waits for a subscription clock and remains task-budget bounded.
- Closed editor, absent adapter, authentication and setup failures are visible.
- Request accepted without observed turn completion is never reported as success.

After deterministic checks, qualify each real adapter with one bounded job and
record runtime version, guard identity, host and observed outcome. A real quota
reset can only be declared passed after actual exhaustion, fresh availability,
observed turn start and completion. Do not deliberately burn allowance to test it.

## Delivered runtime and operator controls

The scheduler is still the sole CLI launcher. Recovery never runs serialized
commands from its ledger or widens a sandbox. A ready intent is claimed inside the
guarded launch transaction; provider session-start and monitor-exit events record
actual start and completion. Successful recovery does not mark a task DONE or
approve code. Existing exact-commit gates, reviewer policy and manager audit
acknowledgement remain required. Source/authorization drift cancels continuation.
Only the scheduler's next generation is accepted; larger generation drift is not.

Quota/health-paused tasks receive intents even before their first launch. Local
capacity errors retain checkpoint/attempt count and use health pause metadata.
Existing records are preserved; recovery tables are added idempotently. Missing
checkouts become host blockers instead of launching a model to diagnose them.

Use from the target project:

```powershell
agentkit recovery status
agentkit recovery capabilities
agentkit recovery cancel INTENT_ID --reason "User stopped this continuation"
```

The dashboard's Recovery and wake delivery panel displays pending intent states.
Accepted/start/completion timestamps are separate in saved state. Expired delivery
claims go to RECONCILING; a restarted watcher cannot submit the same intent again.
Host adapters may register the shared deliver/observe interface, with deterministic
qualification before real use. Claude editor remains UNSUPPORTED; Codex native
remains EXPERIMENTAL. Their capabilities are not inferred from provider availability.

CLI operators can attach the selected provider's external session using its session
environment or explicit --session-ref. Review/recovery must come from that registered
provider/session and possess the existing live manager lease. A project may keep a
Codex native manager and exclude local AI workers; these are project choices.


Native diagnostic renewal is explicit: `--renew` archives only a proven terminal
one-shot; an unknown delivery is never replayed. A filename-safe `--scope` gives
a new user-authorized diagnostic its own receipts. All scopes share the native
thread lock and the same session/authorization delivery claim. The delivered
prompt resolves the project checkpoint from `.ai/runtime`, including scoped
results. Completion is observed until the diagnostic deadline, rather than
assuming acceptance or a short fixed observation period proves success.

## Observed quota reset, 2026-10-04

An authorized Codex VS Code diagnostic observed actual 100% exhaustion, then
fresh account metadata with both 5-hour and weekly windows at 0% following the
operator's reset. One delivery was accepted and its exact submitted turn was
observed running in the existing chat. At that observation, turn completion and
UI health were still unverified; the final receipt remains the authority. This
is evidence for this installed host, not a production guarantee for all editors.

The real exercise exposed and fixed three shared-runtime defects: Unicode/JSON
quota wording had fallen through to a task crash; metadata-only cooldown checks
waited for the old weekly deadline and missed manual resets; and SQLite manager
checkpoints were newer than the native wake's convenience file. Polling is
coalesced per account, paid availability checks still wait for their retry time,
and checkpoint files now mirror committed database memory with job-specific
copies. A mirror failure never discards the authoritative database checkpoint.

CLI and native sessions use the same recovery ledger. In this exercise, a parked
Codex worker also resumed through its guarded CLI launcher. A consumed native
one-shot is never silently replayed; a future diagnostic requires a new qualified
registration, while supervised CLI recovery continues through its scheduler.
