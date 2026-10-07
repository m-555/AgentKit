# Workspaces and dependency provisioning: research and implementation plan

Status: proposed, not implemented. Research date: 2026-10-04.
Applies to AgentKit across projects and providers. Project jobs and model
assignments remain in their target repositories.

## Problem and decision

A fresh agent conversation, a Git checkout and an installed dependency
environment are three different resources. Their lifetimes must be independent.
Current AgentKit creates task worktrees but runs one project-wide setup list.
It shares download caches, prohibits shared installed environments and has
opt-in dependency cleanup after successful integration. This still installs
unnecessary stacks, repeats package extraction and retains failed environments.

Keep worktree isolation for parallel writers. Add configurable grouped storage,
explicit environment profiles and host-managed provisioning. Do not link every
worker to the operator checkout's writable venv or node_modules. Compatibility
with the existing package manager comes before changing it for space savings.

## Research

- [Git worktree](https://git-scm.com/docs/git-worktree): linked checkouts share
  repository data while allowing separate branches on disk. Git does not install
  ignored dependencies. A branch alone does not give a second file checkout.
- [Conductor worktrees](https://www.conductor.build/docs/concepts/git-worktrees)
  and [scripts](https://www.conductor.build/docs/reference/scripts): separate
  agent workspaces have their own branches; setup, run and archive are distinct
  hooks. Dependencies and generated resources are setup responsibilities.
- [agent-worktree](https://github.com/nekocode/agent-worktree): configurable
  grouped workspace directory; explicit merge/cleanup lifecycle; failures retain
  workspaces. It offers dependency-linking examples, but an example is not a
  guarantee of protected shared dependencies on Windows/WSL.
- [Atrium](https://github.com/ZviBaratz/atrium#linked-paths): configurable links
  to ignored dependencies and a per-session isolated alternative. Its README
  explicitly says shared links remain writable and affect all sessions.
- [Vibe Kanban](https://www.vibekanban.com/docs/workspaces/managing-workspaces):
  configurable workspace directory, separate archive/delete semantics and
  dependency cleanup guidance. Worktree removal preserves branches and commits.
  Use current documentation rather than older discussion comments for policy.
- [uv caching](https://docs.astral.sh/uv/concepts/cache/) and
  [link modes](https://docs.astral.sh/uv/reference/settings/#link-mode): shared
  package cache and configurable installation linking. Same-filesystem cache
  placement matters. Package links alone do not prove immutability.
- [pnpm layout](https://pnpm.io/symlinked-node-modules-structure): package files
  come from a content-addressable store with a separate dependency graph per
  project. This is different from linking the entire operator node_modules.
- [Python venv](https://docs.python.org/3/library/venv.html): environments are
  generally not portable. Do not copy a prepared venv to a new path and assume
  its scripts or editable source references still work.

These are design inputs, not proof that any repository satisfies AgentKit's
file-ownership and Windows/WSL confinement requirements.

## Proposed global policy

### 1. Storage and identity

Add a configurable worktree root. Recommended default layout is a grouped
directory outside the source checkout: project identity / workspace identity.
Keep the root on the user-selected drive. Cache and environment locations are
separate settings. Existing recorded workspace paths remain authoritative.

An ignored in-repository worktree directory may be an opt-in mode only after
verification: Git ignore rules, scanners, test collection, watcher exclusions,
sandbox resolved paths and VS Code repository discovery. Ignore rules alone
do not exclude paths from arbitrary recursive scanners.

Moves use Git worktree move with stopped owners and a journal covering Git,
SQLite and the durable workspace registry. Reject dirty or ambiguous moves;
recover interrupted moves before launching workers. Do not silently replace a
recorded path. Never recursively copy the parent repository into its child.

### 2. Environment profiles and readiness

Declare profiles for the checks a task actually runs: static-only, Python tests,
JavaScript tests/build and combined integration. Role defaults may select a
profile, but the declared gate must verify that all required tools are covered.
Dependency-upgrade tasks request a private mutable environment explicitly.

The cache key includes dependency files (including nested workspace manifests),
resolved dependency versions, interpreter/Node/package-manager versions,
platform, architecture, Windows versus Linux transport, setup recipe and
profile. Unlocked requirements cannot certify reproducible reuse by filename
alone. Source references and editable installations must resolve to the current
worker checkout, never to the operator checkout.

Provisioning is host code before agent launch. Builders get only the runtime
needed for source checks; testers get their focused stack; the integration
workspace gets combined dependencies. Static Python checks using only the
standard library should not require a full application venv.

### 3. Reuse without shared mutation

Support two explicit strategies:
- Cache-backed private environments, retaining the project's package manager.
  Prefer copy-on-write where supported; otherwise package-manager cache reuse
  with documented disk costs and protection of shared package content.
- Managed shared environments only where actual write protection is verified
  for every participating runtime. Keep mutable caches, outputs, databases and
  checkout-specific package links private. Refuse this mode if the filesystem,
  sandbox or toolchain cannot enforce the required separation.

Do not equate matching lockfiles, a directory symlink, or a read-only prompt
with enforced immutability. Hard-linked files also share underlying content;
direct in-place writes require protection. Windows and WSL environments are
not interchangeable. Do not migrate npm to pnpm or pip to uv implicitly.

### 4. Setup failure and capacity control

Check capacity and reserve expected provisioning space before scheduling.
Serialize creation of the same cache/profile and install once per valid key.
Record a complete readiness receipt only after success; never trust a stale
success marker after environment cleanup.

Persist setup failure category, recipe/fingerprint, diagnostic and retry policy.
Disk-full and unchanged deterministic failures stop scheduling that setup.
Resume only after verified capacity recovery or an explicit repair/configuration
change. Setup retries do not consume worker attempt or token allowances and
must not send the same package-manager error back to an LLM repeatedly.

### 5. Lifecycle and visibility

Keep fresh conversations for small tasks. Reuse the same recorded checkout for
retries and handoffs. Sequential workspace reuse across different tasks is a
later opt-in optimization requiring ownership, base-commit, cleanliness and
tester-dependency checks; do not assume a fresh session requires a new checkout.

After successful committed integration and stopped ownership, prune private
dependencies according to policy. Remove completed clean checkouts only after
verifying integration evidence and recording durable branch/commit/history.
Preserve partial edits and unmerged work. Never delete active work to make room.

The view must separate workspace state, setup state and AI state. Display
profile, reuse strategy, fingerprint, setup duration, retry count, disk budget,
failure reason and explicit remediation. Host setup has no AI token usage;
unknown model usage stays unknown. Distinguish logical file size from physical
allocation when hardlinks or copy-on-write make those measurements differ.

## Implementation order and evidence

1. Persist deterministic setup blocking and disk-capacity preflight.
2. Introduce role/gate environment profiles and complete cache-key validation.
3. Add prepared-resource reuse with actual Windows/WSL mutation tests.
4. Add grouped storage configuration and journaled migration.
5. Add lifecycle controls and view receipts; document project opt-in migration.

Acceptance checks use small local fixture repositories, not real model calls:
parallel writes remain isolated; unchanged profiles reuse preparation; changed
manifests/runtime/transport invalidate it; cross-worker dependency mutation is
blocked or isolated; cleanup cannot follow links into another checkout; failed
and dirty work survive; interrupted moves recover; unchanged disk failures do
not loop; fresh conversations do not force repeated setup.

Measure cold/warm setup time, setup count, physical disk consumption and AI
calls on the same fixture job before and after. Do not promise a saving
percentage until measured. Preserve exact-commit review and integration gates.

## Implementation status (2026-10-04)

Implemented grouped external storage, journaled moves, gate/role setup profiles,
capacity reservations, durable failure holds, private npm snapshot copies,
cached pinned Python wheels, optional integrated checkout retirement and
workspace setup visibility. See [current guide](../workspaces-and-environments.md).
Protected shared read-only environments remain an alternative design, not an
implemented strategy. The shipped reuse strategies preserve private mutation.
