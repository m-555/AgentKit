---
description: Turn a feature request into a dependency-ordered task graph with non-overlapping file ownership.
argument-hint: [what you want built]
---

Plan this work package: **$ARGUMENTS**

Use the `architect` agent. Before proposing anything:

1. Run `hotspot_report` and read the affected code, so the plan is based on which
   files actually collide rather than on which features sound separate.
2. Check `task_list` for work already in flight — new tasks must not overlap it.
3. Run `conflict_check` on every set of `owned_paths` you intend to create.

Produce:

- the task graph, with `kind`, `owned_paths` and `depends_on` for each task;
- a clear statement of how many agents can run concurrently at each stage;
- any `DECOUPLE` task that has to land before the rest can parallelise.

For an authorized job, save tasks with `task_define`, record module boundaries and
planning decisions with `job_decision`, and activate the current revision with
`job_plan_ready`. For a planning-only request, present the graph without launching.

If the honest answer is "this cannot be parallelised until X is split", say that
plainly instead of inventing parallelism that will collide.
