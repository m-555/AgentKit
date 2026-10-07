---
name: plan-feature
description: Break a feature request into tasks that several agents can execute concurrently without editing the same files. Use when asked to plan a feature package, split work across agents, parallelise development, or decide what can be built at the same time.
---

# Planning work for concurrent agents

The objective is not "maximum agents running". It is **maximum useful concurrency
with minimum integration damage**. Five agents that collide are slower than one
agent that does not.

## Order of operations

**1. Measure, then plan.** Run `hotspot_report` and read the affected code. Feature
names are a terrible guide to independence — "add retry" and "add caching" sound
separate and usually land in the same function.

**2. List the files each piece of work must touch.** Not modules, files. This is
where fake parallelism gets caught.

**3. Classify.**

- `SAFE_PARALLEL` — disjoint files, stable interfaces. Run together.
- `DEPENDENT` — needs another task's output. Sequence with `depends_on`.
- `HOTSPOT` — shares files with other work. **One at a time, no exceptions.**
- `DECOUPLE` — splits a hotspot so the rest can fan out.
- `TEST_ONLY`: own tests; depend on the source commits and interfaces they verify.
- `RESEARCH`: no write paths; return bounded evidence, not implementation.

**4. Stage the graph.** A typical package looks like:

```
  T1  contract / interface        (HOTSPOT, alone)
       |
  +----+----+----+
  T2   T3   T4                    (SAFE_PARALLEL, after T1)
  +----+----+
       |
  T5  cross-cutting work          (DEPENDENT, after T2+T3)
```

Stage one is almost always a single task. Resist starting three agents on day one:
they will all be editing the interface they each need.

**5. Verify the split mechanically.** `conflict_check` every proposed
`owned_paths`. If two overlap, the plan is wrong — fix it now, not at merge time.

## Things that look independent and are not

| Looks separate | Actually shared |
|---|---|
| Two features, two endpoints | the router file they both register in |
| Two providers | the factory / dispatch that selects them |
| Backend + frontend | the schema or generated client between them |
| Two models | the migration chain |
| Two commands | the shared config or CLI parser |

Database schemas, API contracts, migrations, shared types and config files are
**logical hotspots** even when the tasks touch different source files. Treat them
as such.

## What to hand back

- The graph, with each task's kind, owned paths and dependencies.
- Concurrency per stage: "stage 2 runs 3 agents".
- Any decoupling that must land first.
- What you are unsure about.

For an authorized job, persist the graph with `task_define`, explain the split with
`job_decision`, and activate it with `job_plan_ready` for the current revision.
For a planning-only request, present the graph without launching work. Ask the
human only for missing product decisions that cannot be inferred from the request.

## Efficient execution

Each assignment includes exact write files, read context, frozen interfaces,
excluded work, acceptance criteria, owned tests and the next artifact. Keep one
writer per hotspot. Explicit user model pins and fallback triggers override
ranked defaults. The reviewer owns no implementation files. Builders run focused
regressions; integration runs the combined gate after a stable milestone. Avoid
duplicate full runs without a new change or unresolved failure. Save concise
checkpoint decisions, failed approaches and the next action for continuation.


## Separate-task workflow

When the user selects separate implementation/test agents, follow the `separate-tasks` skill and docs/efficient-workflow.md. Assign source-only builder gates and independent TEST_ONLY tasks; this overrides the default combined builder/test guidance above. Manager/reviewer provider and pool counts are project choices.
