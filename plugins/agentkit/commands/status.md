---
description: Show the task graph, who owns what, and anything waiting on a decision.
---

Report the current state of the work:

1. `task_list` — group by status. Show what is READY (could start now), what is
   RUNNING, and what is BLOCKED and on what.
2. Surface any open amendments: these are agents asking for a scope change and
   they are waiting on me.
3. Note any task with `attempts >= 3` — repeated gate failures usually mean the
   task is mis-scoped rather than nearly finished.
4. State how many tasks could run in parallel right now without conflicting.

Keep it short. A table and two or three sentences, not a narrative.
