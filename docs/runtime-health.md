# Runtime health and remaining qualification

The changes are reusable framework code. Repair logs, counters and temporary
fixture repositories are local runtime state, not project feature tasks.

## Checks performed without AI workers

- Full framework suite: 1,030 passed, 7 platform/runtime skips.
- Subsequent affected ownership/launch checks: 108 passed, 2 platform skips.
- Subsequent affected manager/wake recovery checks: 86 passed.
- Ruff and mypy validate the changed source.
- A real Linux zombie is treated as stopped; a live process remains live.
- Stopping an owned Linux worker closes descendant output pipes.
- A real bwrap read-only-authority hook allows owned staged changes, rejects
  foreign staged changes and leaves database contents unchanged.
- A plain Python dummy worker stops at the tool-call cap, preserves its files
  and blocks for manager inspection without changing provider quota.
- Fresh launch arguments for Claude, Codex and local OpenCode are checked
  without invoking their models. Codex worker/control native delegation is off.
- Local dashboard assets/snapshot respond successfully and show stopped owners,
  provider-reported turns and explicitly stale allowance windows.

A passing no-model test is not certification of a live model's write guard.
No new AI workers, delegated tasks or paid inference requests were launched for
these direct repairs. Native root-chat token usage is not exposed by this stream.

## Boundaries still requiring qualification

The mixed Windows-host/WSL-worker transport foundation remains unfinished and
must be integrated and checked before a project uses that arrangement. Keep one
authoritative runtime/database; a Windows writer must not open Linux-owned state.

Native VS Code chat wake remains an opt-in diagnostic over a private protocol.
An idle submission does not prove completion, and no actual Codex quota-reset
recovery has been established. A supervised CLI coordinator can recover from
saved job state; this is a different guarantee from waking an extension chat.

A changed CLI version, hook implementation or isolation mechanism requires its
functional probe before write workers are eligible. Local Qwen remains read-only
until quality and OS isolation qualification justify additional task kinds.

## Applying the efficient workflow

Use the Team setup preview to choose per-role models/effort and export a reviewed
profile. This view does not mutate running sessions or start jobs. Enable
workflow.mode: separate-tasks only with a compatible graph. Old source/test
combined assignments require replanning; completed/cancelled history is retained.
Use execution_paused: true while repairing or qualifying runtime readiness.
Do not interpret elapsed reset times, cached tokens or an AVAILABLE label as a
current subscription percentage. Use reported windows and their observation time.
