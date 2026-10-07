# Opt-in same-chat wake diagnostic

This is an unsupported private VS Code IPC diagnostic for an existing Windows
chat. It is not a production wake or UI-health guarantee. It creates no new model
session and changes no thread settings, permissions, jobs, or worker assignments.
One explicit invocation authorizes at most one delivery attempt to the named chat.

Review the implementation and start the module in a separate hidden process while
the native manager's authorizing turn is active. Do not invoke it as a supervised
worker. The module itself starts no watcher process and reconnects to no IPC owner.
The root manager launches it after review, using `Start-Process -WindowStyle Hidden`
and an explicitly selected Python executable with AgentKit installed.

```text
python -m agentkit.native_wake --root <project> --thread <native-thread-id> \
  --not-before 2026-10-02T12:58:40+02:00 --deadline 2026-10-02T13:13:40+02:00 \
  --poll-seconds 300
```

Both times must include a timezone. The explicit deadline is bounded to 24 hours
from invocation. Not-before is the earliest delivery time, not proof of quota
reset. Official Codex account quota metadata is observed at baseline and each
poll, including before that time. Unknown availability cannot authorize delivery.
An actual observed 100% window is recorded separately from a healthy baseline;
without observed exhaustion this is an idle-wake test, not proven quota recovery.

The module binds to the existing Code.exe pipe process and its creation time,
its confirmed chat owner, and the latest authorizing turn ID. It cancels on a
new turn, owner change, pipe loss, or VS Code restart, and never interrupts an
active turn. Pending approvals, unconfirmed submissions, or pending input prevent
delivery. The authorizing turn may complete, fail, or be interrupted; delivery
still requires an unchanged idle chat and a second fresh state check. History
pagination alone does not revoke the latest turn's authority.

Ignored records are under `<project>/.ai/runtime/native-wake/`, keyed by a hash of
the thread ID. An exclusive persistent arm record prevents duplicate watchers or
restarts. An exclusive claim is flushed before the one native delivery attempt.
A timeout or unknown result consumes that claim; it never retries. Records contain
bounded quota metadata and state summaries, with evidence timestamps and redaction;
conversation text and raw native snapshots are not logged.

`native_turn_completed` records completion of the submitted backend turn only.
`ui_health_verified` remains false. An accepted turn may outlast the bounded
90-second completion observation and remain completion-unverified. The resumed
manager must inspect the result, current manager checkpoint, latest user intent
and authoritative job state. Project-selected roles, models, session limits and
readiness gates govern continuation; the wake message cannot override them.


## Quota failure left in systemError

The 2026-10-04 real diagnostic observed 100% exhaustion, then zero-percent
allowance after reset, but submitted no turn because the unchanged failed native
turn remained in systemError. Quota-only recovery may now attempt that terminal
state after freshly confirmed availability. Idle-only diagnostics cannot. This
change has provider-free regression coverage; a real corrected wake is not yet
proven. See the generic [recovery design](plans/unified-recovery-and-wake.md).

## Registered recovery service

The registered manager uses an independent host process, rather than the
one-shot diagnostic above or a supervisor busy running integration checks. It
checks due registrations on a 20-second cadence; active identity checks are
60 seconds apart and stopped quota availability checks are 300 seconds apart.
Temporary missing, broken or reset pipes during connection setup preserve the
registration for retry. Each retry must revalidate the original Code process,
chat owner, latest authorized turn and job policy. Access denial or changed
authority still fences recovery; unknown turn-delivery outcomes are never resent.

The service health file proves host polling only. Accepted delivery, observed
turn start and observed successful turn completion are separate evidence.
Actual quota-reset continuation in the native extension remains unverified.


A reloaded view may change its internal client owner while the manager is still
working. Registered recovery accepts this handoff only in the same Code process
and exact authorized turn, after proving the previous client disconnected and
rechecking stable identity/state. An active handoff only preserves observation;
it submits no turn and probes no provider. Pending input, a new user turn, any
submitted/claimed recovery, or changed process revokes it. A normal active-to-quota
transition between reads retries observation. The stopped quota path still
requires confirmed provider availability and all existing delivery guards.
