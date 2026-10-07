---
name: verify
description: Run this project's declared checks and interpret the result honestly. Use before committing, before requesting review, before merging, or whenever asked whether the code works, whether tests pass, or whether something is ready.
---

# Verifying work

Every project declares its own commands in `.ai/project.yaml`. Never invent a test
command — run what the project says, via `gate_run`.

## Levels

| Level | When | Meaning |
|---|---|---|
| `fast` | as you work, before each commit | quick feedback, subset |
| `full` | before review, before merge | everything, including slow tests |
| `types` | after signature changes | type checker |
| `build` | before merge if the project ships a build | it compiles |

Use the task's assigned gate for branch verification. In separate-task mode,
builders do not write or run tests: independent TEST_ONLY workers own focused
tests, and the supervisor/integrator runs combined full checks. A source-only
gate is not product verification. Do not assign this skill to a builder as a
reason to override its source-only role.

Outside separate-task mode, use configured focused checks while working. Final
integration needs the full gate. Reuse evidence for the same unchanged commit;
do not repeat full suites in every agent or review turn.

## Reading a failure honestly

The single most damaging habit available to an agent here is making a test pass
without making the code correct. In order of severity:

1. **Editing the test to match the code.** Only legitimate when the task is
   explicitly to change that behavior. During a refactor it is always wrong.
2. **Skipping or marking xfail** to get green. That is not a pass, it is a hidden
   failure with a longer fuse.
3. **Catching the exception** the test was written to detect.
4. **Retrying until it passes.** A flaky test is a finding, not a nuisance.

If the code is right and the test is wrong, say so and stop. Report it; do not
silently correct it.

## Reporting

State what you ran, what passed, what failed, and what you did *not* run. If the
`full` gate was skipped because it takes ten minutes, say that — do not let
"tests pass" stand in for "the fast subset passed".

When a gate fails three times on the same task, stop. Three failures is evidence
that the task is mis-scoped, and the right move is `graph_amend`, not a fourth
attempt.
