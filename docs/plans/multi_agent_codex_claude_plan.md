> Historical design: use [current documentation](../README.md) and the installed
> source for present behavior. This plan is reference material, not a worker brief.

MULTI-AGENT CODEX + CLAUDE DEVELOPMENT PLAN
For extending an existing application where many features touch the same pipeline

1. GOAL
Build a persistent orchestration system that lets multiple Codex and Claude agents work on the same repository without corrupting each other's work, while allowing sessions to be replaced when context is exhausted or a process dies.

The key idea is simple:
- Agent sessions are temporary workers.
- Git, task state, checkpoints, architecture documents, and tests are the permanent memory.
- Parallelism is used only when work is truly independent.
- Shared/hot files are controlled by one implementation owner at a time.


2. CORE ARCHITECTURE

                        YOU
                         |
                         v
              +--------------------+
              | Architect / Planner |
              +----------+---------+
                         |
                  task dependency graph
                         |
                         v
              +--------------------+
              |    Orchestrator     |
              | Python + SQLite     |
              +----------+---------+
                         |
      +------------------+------------------+
      |                  |                  |
      v                  v                  v
  Codex worker       Claude worker      Test/Review worker
  worktree A         worktree B         worktree C
      |                  |                  |
      +------------------+------------------+
                         |
                   Merge / Integration
                         |
                  integration branch
                         |
                    test gate
                         |
                        main

Do not let multiple implementation agents edit the same working directory.
Every coding worker gets its own Git worktree and branch.


3. MOST IMPORTANT RULE FOR YOUR USE CASE
When many new features affect the same part of an existing pipeline, do NOT assign every feature to a different coding agent simultaneously.

Classify work into three categories:

SAFE_PARALLEL
- Different modules or cleanly separated files.
- Can run at the same time.

DEPENDENT
- Feature B requires Feature A first.
- B waits until A is integrated.

HOTSPOT
- Multiple tasks need the same files, classes, schemas, or core logic.
- Only one implementation agent owns that hotspot at a time.
- Other agents can analyze, test, research, or prepare designs in parallel.

Example:

Existing pipeline:
  pipeline/generator.py
  pipeline/models.py
  pipeline/api.py
  pipeline/queue.py

Requested features:
  A. multi-model support
  B. retry logic
  C. reference images
  D. prompt templates
  E. cost tracking
  F. caching

Bad scheduling:
  Codex A -> generator.py
  Claude B -> generator.py
  Codex C -> generator.py
  Claude D -> generator.py

Good scheduling:
  Lead implementation agent -> owns generator.py and api.py
  Agent 2 -> designs retry behavior and edge cases
  Agent 3 -> writes/updates tests
  Agent 4 -> analyzes database/API impact

The lead consumes those outputs and makes the shared-file changes sequentially.


4. ARCHITECT FOR PARALLELISM
Before launching many implementation agents, refactor large hotspots into stable interfaces.

Instead of:
  generation/generator.py   # huge file edited by everyone

Prefer:
  generation/
    base.py
    orchestrator.py
    providers/
      openai.py
      claude.py
      google.py
    retry/
      manager.py
    caching/
      cache.py
    accounting/
      costs.py

After the common abstraction is merged, separate agents can safely own different modules.

Example dependency graph:

              A: common generator abstraction
                 /        |        \
                /         |         \
               v          v          v
       B: OpenAI       C: Claude    D: Retry
               \          /
                \        /
                 v      v
              E: cost tracking

A runs first. B, C, and D may then run in parallel. E waits for B and C.


5. REPOSITORY STRUCTURE
Recommended control files:

repo/
  AGENTS.md
  CLAUDE.md
  .ai/
    PROJECT.md
    ARCHITECTURE.md
    RULES.md
    ownership.yaml
    tasks.db
    runtime/
      task-101/
        handoff.json
        notes.md
      task-102/
        handoff.json
    agents/
      architect.md
      codex-backend.md
      claude-frontend.md
      tester.md
      reviewer.md

PROJECT.md
- Stable project facts.
- Stack, commands, important paths, environment assumptions.

ARCHITECTURE.md
- Module boundaries.
- Important data flow.
- API/schema contracts.
- Where new functionality should be added.

RULES.md
- Coding conventions.
- Required tests.
- Files or behavior agents must not change without approval.
- Commit/checkpoint requirements.

ownership.yaml
- Current hotspot ownership and allowed paths.

AGENTS.md / CLAUDE.md
- Provider-specific entry instructions.
- Both should point to the same .ai source-of-truth files.


6. TASK DATABASE
Use SQLite initially. It is enough for a local orchestrator.

Suggested task fields:
  id
  title
  description
  status
  type                 # SAFE_PARALLEL / DEPENDENT / HOTSPOT
  assigned_agent
  branch
  worktree
  owned_paths
  dependencies
  priority
  heartbeat
  last_commit
  checkpoint_path
  attempts
  created_at
  updated_at

Task states:
  PENDING
  READY
  RUNNING
  BLOCKED
  REVIEW
  MERGING
  DONE
  FAILED
  STALLED

Example:

Task 101
  title: Refactor generator abstraction
  type: HOTSPOT
  status: RUNNING
  agent: codex-lead
  owns: pipeline/generator.py, pipeline/api.py

Task 102
  title: Add OpenAI provider
  type: SAFE_PARALLEL
  depends_on: 101

Task 103
  title: Add Claude provider
  type: SAFE_PARALLEL
  depends_on: 101

Task 104
  title: Add retry manager
  type: SAFE_PARALLEL
  depends_on: 101

Task 105
  title: Add cost tracking
  depends_on: 102, 103

Scheduler behavior:
  101 runs
  -> 101 passes review and integrates
  -> 102 + 103 + 104 become READY and start concurrently
  -> 102 + 103 complete
  -> 105 becomes READY


7. GIT STRATEGY
For a larger feature package, use an integration branch.

main
  |
  +-- feature/video-pipeline-v2
        |
        +-- agent/provider-openai
        +-- agent/provider-claude
        +-- agent/retry
        +-- agent/cache

Each coding worker uses a separate worktree:

  git worktree add ../wt-provider-openai -b agent/provider-openai feature/video-pipeline-v2
  git worktree add ../wt-provider-claude -b agent/provider-claude feature/video-pipeline-v2

Workers never merge directly to main.
They submit to the integration branch through a review/merge gate.

For HOTSPOT work, keep one implementation branch active for that shared area until the common change is integrated.


8. OWNERSHIP + LOCKING
Use both static ownership and runtime locks.

Static ownership example:

  generation-core:
    owner: codex-lead
    paths:
      - generation/base.py
      - generation/orchestrator.py

  provider-openai:
    owner: codex-provider-openai
    paths:
      - generation/providers/openai.py

Runtime lock example:
  resource: generation/base.py
  owner: task-101
  lease_until: timestamp

Before a worker starts editing, the orchestrator checks whether its requested paths overlap another active task.

If overlap exists:
- SAFE_PARALLEL becomes BLOCKED, or
- the architect resplits the task, or
- one task becomes a non-coding research/test task.

Do not rely only on file locks. Logical conflicts can occur across different files through shared schemas, interfaces, migrations, or API contracts.


9. AGENT ROLES
A practical first version needs about 4-6 roles, not dozens.

ARCHITECT / PLANNER
- Reads the current repo and requested feature package.
- Produces task graph.
- Detects shared hotspots.
- Defines contracts before parallel work begins.

LEAD IMPLEMENTER
- Owns active hotspot/core pipeline changes.
- Integrates research and test feedback.

PARALLEL IMPLEMENTERS
- Work only on isolated modules after interfaces are stable.

TEST AGENT
- Builds regression tests and new feature tests.
- Can often work in parallel from agreed behavior/contracts.

REVIEW AGENT
- Reviews diffs against task definition and architecture.
- Checks for accidental scope expansion.

INTEGRATOR / MERGE AGENT
- Rebases or merges approved branches.
- Runs full checks.
- Resolves integration-level conflicts.
- Is the only automated role allowed to merge to the integration branch/main.

Codex and Claude can be assigned to any role based on which performs better for that task; do not hard-code architecture around one provider being permanently 'the boss'.


10. PERSISTENT MEMORY AND SESSION REPLACEMENT
Never make the model's conversation history your only memory.

Each worker periodically writes a checkpoint:

.ai/runtime/task-101/handoff.json

Example:
{
  "task": 101,
  "goal": "Refactor generator abstraction",
  "status": "running",
  "completed": [
    "Added BaseGenerator interface",
    "Moved common request validation"
  ],
  "remaining": [
    "Update API adapter",
    "Run integration tests"
  ],
  "files_changed": [
    "generation/base.py",
    "generation/orchestrator.py"
  ],
  "tests": "41 passed, 2 pending",
  "last_commit": "ae81c61",
  "important_decisions": [
    "Provider classes return normalized GenerationResult"
  ],
  "next_action": "Update API adapter to call GenerationOrchestrator"
}

Also require frequent logical commits:
  commit -> checkpoint -> continue

A new session should be able to reconstruct its job from:
  1. AGENTS.md or CLAUDE.md
  2. .ai/PROJECT.md
  3. .ai/ARCHITECTURE.md
  4. task record from SQLite
  5. handoff.json
  6. git status
  7. git diff
  8. recent git log

The replacement agent receives a short continuation instruction:

  You are continuing task #101.
  Read the project instructions and task checkpoint.
  Inspect git status, diff, and recent commits.
  Continue from next_action.
  Do not redo completed work.
  Stay within the owned paths unless the task must be escalated.

This makes individual Claude/Codex sessions disposable.


11. HEARTBEAT AND SUPERVISION
Each active worker updates a heartbeat in SQLite after meaningful activity or on a timer.

Supervisor checks:
- Is the process alive?
- Is heartbeat stale?
- Has task status changed?
- Is there an uncommitted working tree for too long?
- Did tests fail repeatedly?
- Did the worker request files owned by another task?

If stale:
  RUNNING -> STALLED

Then the orchestrator may:
- inspect state,
- create a checkpoint from available data,
- terminate the worker,
- start a replacement session using the same worktree/branch/checkpoint.

This handles context exhaustion, CLI crashes, machine restarts, and accidental session loss.

It does NOT bypass provider usage/rate limits. If the provider account is rate-limited, the task remains blocked until capacity returns or a permitted alternative worker/model is assigned.


12. ORCHESTRATOR LOOP
Conceptual loop:

while True:
    refresh_agent_processes()
    refresh_heartbeats()

    mark_stalled_workers()
    recover_stalled_tasks()

    update_dependency_states()

    for task in ready_tasks:
        if no_path_or_contract_conflict(task):
            allocate_worktree(task)
            acquire_locks(task)
            launch_best_worker(task)

    collect_finished_tasks()
    send_finished_tasks_to_review()

    for approved_task in merge_queue:
        integrate(approved_task)
        run_required_tests()
        release_locks(approved_task)

    update_task_graph()

The scheduler should be dependency-aware, ownership-aware, and resource-aware.


13. FEATURE REQUEST WORKFLOW
When you say:
  "Add retry, caching, reference-image support, cost tracking and two new providers to the video-generation pipeline."

The system should NOT immediately spawn five coders.

Stage 1 - Inspect
Architect reads the affected code and tests.

Stage 2 - Plan
Architect identifies:
- hotspot files,
- common abstractions,
- contracts,
- migrations,
- test requirements,
- dependency graph.

Stage 3 - Stabilize shared core
One lead agent makes any common refactor required for safe parallel work.

Stage 4 - Parallel execution
Only independent modules are assigned concurrently.

Stage 5 - Review
Every branch is reviewed against its task and contracts.

Stage 6 - Integration
Merge agent combines approved work into the feature integration branch.

Stage 7 - Full validation
Run unit, integration, lint/type checks, migrations, and relevant end-to-end tests.

Stage 8 - Main
Only the validated integration branch is merged to main.


14. CONFLICT PREVENTION RULES
1. One worktree per coding worker.
2. One active owner per hotspot.
3. Define interfaces before parallel implementation.
4. Do not parallelize tasks just because they have different feature names.
5. Treat database schemas, APIs, migrations, shared types, and config as logical hotspots.
6. Workers must commit small logical milestones.
7. Workers must checkpoint before stopping/restarting.
8. Workers cannot merge directly to main.
9. Full tests run after integration, not only on individual branches.
10. If a worker discovers it needs an unowned shared area, it stops and requests a task-graph change instead of silently editing it.


15. FIRST IMPLEMENTATION STACK
Keep version 1 simple:

- Python orchestrator
- SQLite state database
- Git worktrees
- subprocess for Codex CLI / Claude Code
- tmux optional for process visibility
- JSON checkpoint files
- YAML ownership file
- pytest / project-specific test runner
- simple polling supervisor

Do not start with Kubernetes, Redis, a message broker, or a large distributed system unless you actually need remote workers.


16. PHASED BUILD PLAN

PHASE 1 - Reliable local workers
- Create .ai project files.
- Create SQLite task schema.
- Build worktree create/remove helpers.
- Launch one Codex or Claude worker from a task.
- Record PID, branch, worktree, status, heartbeat.

PHASE 2 - Recovery
- Add checkpoint format.
- Detect dead/stale workers.
- Relaunch a worker from checkpoint.
- Verify the new session can continue without old conversation history.

PHASE 3 - Dependency scheduler
- Add task dependencies.
- READY/BLOCKED transitions.
- Run multiple independent workers concurrently.

PHASE 4 - Hotspot protection
- Add owned_paths.
- Detect overlapping paths.
- Add runtime leases/locks.
- Add architect classification: SAFE_PARALLEL / DEPENDENT / HOTSPOT.

PHASE 5 - Review and merge queue
- Automated diff review task.
- Test gates.
- Controlled integration branch merges.
- Rollback/retry when integration fails.

PHASE 6 - Smarter planning
- Architect analyzes requested feature packages.
- Automatically proposes module boundaries and dependency graph.
- Human approval can remain optional depending on risk.


17. RECOMMENDED OPERATING MODEL
For your situation - repeatedly extending an already-built application - use this default:

- 1 Architect/Planner
- 1 Lead implementation agent for the current hotspot
- 1-3 parallel implementation agents only after boundaries are stable
- 1 Test/Review agent
- 1 Integrator

The number of agents should grow only when the task graph shows genuine parallel branches.

The objective is not "maximum agents running."
The objective is "maximum useful concurrency with minimum integration damage."


18. FINAL DESIGN PRINCIPLE
Treat Codex and Claude as replaceable compute workers around a persistent engineering system.

Permanent:
- Repository
- Git history
- Tests
- Architecture docs
- Task database
- Ownership/locks
- Checkpoints
- Integration state

Temporary:
- Individual Codex sessions
- Individual Claude sessions
- Their context windows
- Their local reasoning history

With this design, a worker can die, hit its context limit, or be replaced by another model without losing the project's operational state.
