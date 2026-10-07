# Live agent visibility

Run `agentkit live` in an initialized target repository to inspect saved runtime
state. `agentkit live --follow` refreshes every two seconds until Ctrl-C;
`--poll 5` changes the interval. `--task 7` selects one task while retaining
processes without a task, including coordinator and reviewer sessions. `--json`
emits one JSON snapshot per refresh for other consumers.

The monitor displays the recorded provider, launch model, effort, role,
monitor and child PIDs, public provider session ID, worktree, task state and
heartbeat. It includes control agents even when no tasks have been defined.
Requested and provider-observed model/effort remain separate, with an explicit
verification flag. Older runtime databases retain launch metadata as requested
values rather than presenting it as observed provider evidence.
The latest saved task gate includes its checked commit; a passing historical
gate does not establish that newer work has passed. Task progress uses recorded
workflow states rather than an invented completion percentage.

External managers registered by the backend are shown separately, including their
reported model, effort, public session ID, PID, lease heartbeat/TTL, and recovery,
acknowledgment and audit epochs. Registration alone does not verify provider model
identity. Released, stale and crashed registrations remain visible. Authentication
hashes, manager checkpoint payloads and recovery evidence are never read.

Recorded `RUNNING`/`STARTING` processes with a dead monitor PID appear as
`CRASHED`, or `ORPHANED` if the child remains alive. Every process row checks
both recorded PIDs, including terminal rows. If either PID is still alive after a
terminal status, the view shows `TERMINAL_PID_ALIVE` with a liveness risk flag and
retains the recorded status. `FINISHED` appears as `STOPPED` only when a recorded
PID has been checked and neither PID appears alive; without either PID it shows
`EXIT_UNCONFIRMED`. Recorded failures remain `FAILED` when no PID appears alive.
Missing PIDs have unknown liveness. PID checks are best effort and cannot prove
that a reused PID still belongs to the original agent.
Quota-paused tasks show `WAITING_QUOTA`. Saved five-hour and weekly windows show
usage and reset timestamps separately. A reset or retry timestamp permits an
availability check; it does not establish recovered allowance. Unknown times
remain unknown, and no provider checks or inference requests run from this view.

Each process's recent tool activity comes from its actual
`.ai/runtime/process-<id>/events.jsonl`, including Codex command/file/MCP events,
Claude tool-use blocks, OpenCode tool-use events, and normalized lifecycle/tool
events. Only tool names, event kinds, lifecycle states and timestamps are
projected. The monitor never displays agent messages, reasoning, prompts, command
arguments, tool inputs or outputs, raw errors, launch environments, credentials
or authentication tokens. Public session identifiers use known UUID/OpenCode
formats; unknown or credential-shaped values are withheld. Metadata also uses
the existing credential redaction helper and strips terminal control characters.

Monitoring opens the existing SQLite database with `mode=ro` and a query-only
connection. It creates no missing project/database, applies no migrations,
updates no heartbeat, and never reconciles or approves work. Missing/old tables,
an unreadable database and absent process logs produce useful empty/error views.
New processes and logs are discovered on every poll. No terminal UI dependency
is required; plain frames also work when redirected to a log.

## Public messages in CLI viewers

The optional Windows CLI viewers display public Claude text blocks and Codex
agent messages, alongside safe tool
labels. These read the saved stream; they request no additional model narration.
Hidden reasoning, prompts, command arguments, tool inputs/outputs and raw stderr
remain excluded. Credentials are redacted before text truncation. Browser text
is rendered with textContent, never interpreted as HTML or shell commands.

Each viewer shows its role, numbered task and worktree. Public messages appear
when the provider emits them; silent work does not acquire invented narration.
The backend text tail is limited to 256 KiB / 60 records.
The ordinary `agentkit live` metadata snapshot remains transcript-free.

## Actual window mirrors and manager input

The dashboard's **Live agent screens** displays PNG frames from each registered
Windows console window, including its fonts, colors and layout. It does not
reconstruct an HTML terminal from tool events. Pause display stops frame requests;
it does not pause the agent. Full screen enlarges the selected monitor. Workers
are always read-only. Restoring a minimized console allows frames to refresh.
Missing/closed windows are reported as unavailable; the last frame is explicitly
marked retained. AgentKit does not substitute an invented terminal image.

Supervised workers currently use Claude print/Codex exec streams. Their visible
window is AgentKit's console viewer, not an interactive provider TUI. The mirror
shows that actual viewer window. It cannot turn a noninteractive worker into a
TUI or show a CLI that has no registered visible window. A native VS Code chat
has no AgentKit-owned CLI window; its messages remain in the editor.

Captures are limited to the registered window's client area, not the desktop.
The exact title, window-owning PID and viewer PID birth identities are checked
before and after capture. A different terminal tab, reused PID or changed manager
lease stops capture/input. Windows Terminal support depends on a uniquely titled
visible host window; an unsupported renderer returns unavailable. The capture
helper times out after three seconds because Windows PrintWindow is synchronous.
Capture currently requires Windows; it does not claim Linux GUI support.

Only an existing **interactive CLI manager** can opt into keyboard delivery:

```
agentkit --path PROJECT terminal register-manager --job JOB --pid EXACT_CLI_PID
```

This command requires the current fresh external-manager lease, matching session
and PID, and an interactive native Windows Claude/Codex CLI process. It creates
no model session and performs no manager handoff. Print/exec sessions, workers,
editor bridges and WSL-only managers are excluded. Keep using the documented
manager handoff before registering a replacement; never register a second owner.

When the dashboard runs with `--operator-controls`, the manager monitor accepts
one text line (up to 4000 characters) and Enter. It writes to that exact console's
input buffer, without changing desktop focus or sending global keystrokes.
Control sequences and arbitrary worker input are refused. Local origin and the
current operator token are required. There are no automatic retries: a timeout
can mean delivery occurred, so inspect the manager before resending. A delivered
input receipt proves keyboard delivery, not model acceptance or task completion.
This is line input, not a complete browser PTY with shortcuts and terminal resize.
Without a registered CLI manager the input stays unavailable. The root editor
chat remains manager until the user authorizes a verified handoff.

## Project usage

`agentkit --path PROJECT usage` reads project totals without inference. Totals
include every recorded launch, not just the dashboard's most recent 200 rows:
workers, testers, control sessions, failed attempts and provider continuations.
Each process contributes once. Duplicate message snapshots/final summaries are
deduplicated within that process by the existing provider counter reader.

Host completion saves a numeric receipt in `.ai/runtime/process-ID/usage.json`.
The supervisor captures stopped logs missed during a monitor crash. Receipts
survive reader restarts and log retirement. For older projects run
`agentkit --path PROJECT usage --backfill` once. This scans stopped logs on the
host and writes receipts; it starts no job or model session. Each full scan is
bounded to 64 MiB and reports partial coverage if the limit is exceeded.

Normalized input includes Claude reported input + cache reads + cache writes;
Codex reported input already includes its cached input. Recorded total is that
input plus output. Thinking is a breakdown within output, never added again.
Provider/role breakdowns show raw counters and their reporting-launch counts.
Missing fields/sessions stay unknown; partial coverage never becomes a zero.
An externally registered chat without counters is explicitly unmetered.
Unrecorded setup/probe calls and old logs already deleted cannot be reconstructed.
These are observed token counts, not a billing or account-allowance calculation.

Usage and window reads do not backfill, change tasks, renew leases or request
allowance from providers. Explicit local operator actions remain separate.
Its Worktrees and integration panel also shows the combined branch's actual
checkout and commit. Follow the target project's preview guide from that path.

Reads are bounded to 200 recent records per table, 256 KiB of launch metadata per
process and 64 KiB/200 lines from each stream, displaying six recent safe events.
The task filter applies before those row limits. Partial, malformed and deeply
nested stream records are skipped without echoing their contents. JSON exposes
stream tail/truncation and skipped-record metadata. Symlinks escaping the target
repository are refused. Poll intervals must be finite and at least 0.1 seconds.

`agentkit.live.run` also accepts injected output/sleep callbacks and an optional
iteration limit for deterministic tests and embedding. A one-shot unavailable
runtime returns 1; invalid polling parameters return 2; Ctrl-C returns 0.
