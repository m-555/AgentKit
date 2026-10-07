# Worktree and committed integration policy

The operator checkout stays on its current branch. Workers receive one stable
worktree under the configured external `wt-<repo>` directory and one `agent/<bounded-spec-id>-<stable-hash>` branch per task.
The hash distinguishes long or similar task IDs. Retries and provider handoffs
reuse the recorded path and branch; they never create a competing checkout.
Existing explicitly recorded worktree paths and branches remain valid.

Before Git creation, AgentKit writes an atomic reservation in
`.ai/runtime/workspaces.json`. It records the stable spec ID, runtime task number,
job, path, branch, phase and commit/integration evidence. That manifest survives
a rebuilt task database. Back up it together with `.ai/` and the Git repository.
Git's own worktree and branch inventory independently discovers unregistered
agent branches, missing checkouts and moved checkouts. Discovery never deletes,
prunes, resets, force-adopts or silently redirects unfinished work.

Use the dashboard's **Worktrees and integration** panel, or the read-only
`workspaces` MCP tool, to inspect the inventory. Refresh is explicit and makes no
model calls. Unregistered branches need ownership recovery before a new task
adopts them. Task IDs are execution numbers; spec IDs identify durable work.

Workers commit bounded changes to their assigned branch. WSL workers use the
host `task_commit` MCP tool, which requires the current running generation,
matching branch, and clean ownership audits of all changed and staged files.
Commits preserve normal Git hooks. A commit is not review or successful testing.

The Python supervisor automatically integrates a task when its current job plan
is active, no process still owns it, recovery holds are clear, the exact commit
has the required approval, and task checks pass. A private integration worktree
runs combined regression, lint and configured full checks. The exact approved
commit is merged into the configured integration branch. A changed approved HEAD,
uncommitted files, out-of-scope changes, stale contracts or conflicting changes
hold the task. Failed combined checks restore the previous integration commit.

After an interrupted integration, the supervisor retries the preserved commit
through the same approval, ownership and full-check path. If Git already contains
that exact commit it performs the combined check without merging it twice.
A recovery epoch still needs its required audit and acknowledgement.

A higher operational contract-lock version may still describe exactly the same
interface. AgentKit compares both recorded snapshots, their complete tracked
file sets and actual bytes before accepting that case. Missing evidence, changed
hashes, added/removed files or checkout drift keep the contract barrier closed.
The task retains its original pin and exact-commit approval. An authenticated
planner may use `integration_retry` for this specific stale-version failure or a
repaired combined-check failure; code reruns full integration checks without
launching another worker. A real contract change still requires replanning.

Accepted integration does not automatically publish, push or merge into protected
`main`/`master`. Project delivery remains a separate human decision under AgentKit's
existing protected-branch rule. Completed branches remain available. Completed worktrees remain by default;
opt-in archival retires only clean integrated checkouts and records their history.
Crashes, partial edits and unmerged commits are preserved.

Builders change source; separate testers change tests; review follows the chosen
human or AI review mode. These lifecycle rules are implemented by code and apply
to future projects regardless of which provider plans or reviews them.

## Completed worker environments

Set `worktree_environment_cleanup: true` to reclaim a completed worker's
reproducible `venv`, `.venv` and `node_modules` after successful integration.
Cleanup verifies the recorded Git checkout, unchanged clean source commit,
provisioning marker and stopped ownership. It refuses tracked dependency source
and linked environment roots. Package links below an environment are removed
without traversing their targets. Branches, worktree source and registry records
remain available for review. Cleanup failure is recorded and does not revoke
integration. Active, failed and paused workers keep their environments.

For grouped storage, explicit migration, setup profiles and optional checkout
retirement, see [Workspaces and environments](workspaces-and-environments.md).
