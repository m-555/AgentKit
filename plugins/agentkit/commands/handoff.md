---
description: Save a checkpoint good enough for a different agent to continue without your context.
---

Write a checkpoint for the current task.

Hooks already capture git state automatically. What they cannot capture is what you
know and the diff does not show — so record that:

- **completed** — what is actually finished, not what is started.
- **remaining** — concrete next steps, in order.
- **next_action** — the single thing the next session should do first.
- **decisions** — choices you made that a replacement would otherwise re-litigate,
  with the reason. "Provider classes return a normalised MediaResult so the
  registry does not need per-provider branches" is useful; "refactored the code"
  is not.

Also record anything you tried that did **not** work. A replacement agent
re-discovering a dead end is the most expensive kind of lost context.

Commit any uncommitted work first — a checkpoint that points at an uncommitted tree
cannot be resumed cleanly.

Then call `checkpoint` with those fields.
