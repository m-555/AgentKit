---
description: Start or resume work on a task, with its brief, scope and gates loaded.
argument-hint: [task id]
---

Work on task **$ARGUMENTS**.

1. Call `brief` for that task. Read all of it, including the paths other agents
   currently hold.
2. If there is a checkpoint, continue from `next_action`. Do not redo completed work.
3. Delegate to the agent matching the task's `role` (`implementer`,
   `lead-implementer`, `decoupler`, `test-author`).
4. Stay inside `owned_paths`. If you are blocked, use `lease_request` or
   `graph_amend` — never route around the block.
5. Commit small logical milestones as you go.
6. Before stopping: run `gate_run`, then `checkpoint`, then `task_status REVIEW`
   with the gate output as evidence.
