# Windows host with a Linux Claude worker

AgentKit retains one state authority per project. In this transport the Windows
host alone opens `.ai/tasks.db`, runs MCP, Git commits, task checks, and integration.
The WSL worker runs Linux Claude and forwards hooks and MCP through a private
user-only Unix socket. It never opens the Windows database or starts Windows Python.

Opt in in the target project's `.ai/project.yaml`:

```yaml
transports:
  claude-code:
    type: wsl
    distro: Your-Ubuntu
    user: worker
    python: /home/worker/.local/share/agentkit/venv/bin/python
    claude: /home/worker/.local/share/claude/versions/2.1.286
```

Install the same AgentKit source/package in Windows and this Linux virtualenv.
Linux executables must be absolute paths outside `/mnt`; Windows programs and
ambiguous paths are refused. The Windows authority must own the project database.
Do not open a Linux-owned live database from Windows. Stop its supervisors and
migrate it deliberately in its owning runtime instead.

The Linux user's Claude settings must enable the sandbox with
`failIfUnavailable: true` and `allowUnsandboxedCommands: false`. A generated hook
file is not proof of isolation. Run the functional probe on the configured Windows
project to certify this actual transport. A Linux-only or native Windows proof
cannot authorize it. Binary, guard/launcher source, host Python, distribution,
user and transport configuration changes invalidate cached confinement evidence.

The worker uses `task_commit` and `gate_run` MCP tools for Git and project checks.
Linux Git never needs to modify Windows Git metadata. The host audits every
changed/staged file before commit and retains normal Git commit hooks. All project
checks execute in the host's assigned worktree. Developers keep their own checkout.

Protocol version 1 uses bounded authenticated directional frames and monotonic
sequence numbers. Claude JSONL remains native; lines resembling control frames
are wrapped. Hook errors, timeouts, malformed payloads and integrity failures
refuse writes. There is no retry of a request with an unknown mutation outcome.
MCP sessions remain separate, with at most two channels per worker.

The MCP stdio proxy is duplex. It forwards subscription acknowledgments,
notifications and responses while continuing to accept client requests and
responses to server requests. A long-lived `subscriptions/listen` stream must
not block `tools/list` or a tool call. Both modern discovery and legacy
`initialize` handshakes work. Private send/poll/close envelopes travel inside
the existing authenticated v1 message payload; the native MCP messages are
forwarded unchanged to the Windows server. Polling uses code and does not call
a model. Each channel has a bounded queue of 32 frames; malformed, oversized
or overflowing output fails closed. Client EOF closes the host MCP process
and frees its channel slot, retaining the two-active-channel cap.

The Linux worker records boot ID, PID/start time and the Claude process group.
Host loss terminates that group. Normal exit requires the Linux termination
report and the WSL wrapper exit to agree. Recovery checks Linux identity before
releasing an abandoned Windows monitor's ownership; a dead Windows PID is
insufficient evidence for a competing continuation. Uncertain ownership holds
work for inspection.

Windows Codex continues to use its native sandbox. Its write launcher performs
metadata-only exact hook-definition review and trust verification. Trust overrides
are session-scoped to AgentKit's shipped command; no global hook-trust bypass is
used. Live confinement certification remains required in addition to metadata.

## Qualification and cost

Offline tests cover replay/reflection, malformed runtime/path input, credential
filtering, migration, lost workspace inventory and interrupted integration.
A real Windows/WSL fixture can prove hook/MCP routing and shutdown without invoking
models. Functional confinement requires small real tool attempts with an allowed
positive control, unchanged canaries and provider/authority denial evidence.

Healthy Claude workers are not pinged by inference. After a recorded allowance
reset, a bounded, tool-free availability request can confirm recovery. A reset
time alone never marks the account available. This worker recovery does not prove
that a particular VS Code extension chat can automatically start another turn;
that native quota-reset scenario still requires observed turn-completion evidence.
