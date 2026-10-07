---
description: Merge independently approved tasks behind the combined full gate.
argument-hint: [optional queue limit]
---

Run `agentkit integrate` to process the INTEGRATION_READY queue. The supervisor
does this automatically during `agentkit run --watch` or `agentkit job start`.

Every task needs an independent PASS for its current clean commit, a passing task
gate, and a clean scope audit. The deterministic integrator merges the approved
commit into its dedicated integration checkout and runs the combined full gate.
Failures preserve the worker's branch and return evidence for correction.

Report what merged, what failed, and which tasks became ready. Do not bypass the
gate or merge to a protected branch.
