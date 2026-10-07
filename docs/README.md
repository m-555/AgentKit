# AgentKit documentation

Start with the repository [README](../README.md) for installation and a first job.
These guides describe current behavior; the source and project configuration
establish what runs. Read only the guide needed for the current task.

## Current guides

| Topic | Guide |
|---|---|
| Human or AI review, one planner/manager | [Review workflows](review-workflows.md) |
| Small fresh builders and separate testers | [Efficient workflow](efficient-workflow.md) |
| Bundled skills and project-specific instructions | [Skills and project guidance](skills-and-project-guidance.md) |
| Provider/model choices, handoffs and quota recovery | [Recovery and model policy](recovery-and-model-policy.md) |
| Windows authority with WSL Claude | [WSL transport](wsl-transport.md) |
| Grouped storage, setup profiles and safe moves | [Workspaces and environments](workspaces-and-environments.md) |
| Stable worktrees, commits and gated integration | [Worktree integration policy](worktree-integration-policy.md) |
| Session visibility and counters | [Live monitor](live-monitor.md) |
| Compact manager queries and environment identity | [Manager efficiency](operations/manager-query-efficiency.md) |
| Runtime health checks | [Runtime health](runtime-health.md) |
| Native chat wake diagnostics and limits | [Native wake](native-wake.md) |
| Developing and checking AgentKit itself | [Development](development.md) |

## Plans and reference reports

- [Unified recovery and wake](plans/unified-recovery-and-wake.md): shared policy for workers/managers and provider/host adapters; implementation status and gaps are explicit.
- [Workspace and environment provisioning](plans/workspaces-and-environment-provisioning.md): accepted design; implemented storage, private preparation reuse and lifecycle controls.
- [Plan v3](plans/PLAN_V3.md): historical architecture and invariants.
- [Plan v2](plans/PLAN.md) and [original plan](plans/multi_agent_codex_claude_plan.md): earlier proposals.
- [Sandbox review](reviews/SANDBOX_REVIEW.md): preserved historical findings.
- [Qwen evaluation](evaluations/QWEN_EVALUATION.md): dated, bounded model qualification.
- [Glossary](GLOSSARY.md): introductory vocabulary; older model examples are historical.

Plans and evaluation reports do not certify today's runtime or override user
choices, current code, accepted project decisions or configured gates.

## Where instructions live

Human documentation lives here. Executable plugin instructions stay in
`plugins/agentkit/skills`, `plugins/agentkit/agents` and `plugins/agentkit/commands`.
Moving them into docs would break plugin discovery and wheel packaging.
The native `.agents/skills` and `.claude/skills` entries are short pointers to
those canonical skills. Target-project skills and jobs stay in the target repo.
