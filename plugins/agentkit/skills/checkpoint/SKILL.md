---
name: checkpoint
description: Save work state so a different session can continue without your context. Use before stopping, when context is filling up, before a risky change, or when asked to hand off, save progress, or write a handoff note.
---

# Checkpointing

Your context window will be lost — to compaction, a crash, or the end of the
session. The checkpoint is what survives. Write it for a competent agent who has
never seen this conversation.

## What the hooks already do

`PreCompact` and `Stop` hooks capture git state automatically: changed files, last
commit, branch. You do not need to record those.

## What only you can record

**decisions** — choices with reasons. The diff shows *what* you did; the checkpoint
must explain *why*, or the next agent will undo it.

> "Providers return a normalised MediaResult rather than raw provider payloads,
> so the registry needs no per-provider branching."

**dead ends** — what you tried that did not work, and why. This is the most
expensive thing to lose, because the next agent will otherwise spend the same hours
proving the same thing does not work.

**next_action** — one concrete sentence. Not "continue the refactor" but "update
`routes/video/__init__.py` to include the new images router, then run the fast gate".

**remaining** — ordered, concrete steps.

**completed** — what is *finished*, not what is started. Overstating this is how a
replacement agent skips unfinished work.

## Before you checkpoint

Commit. A checkpoint pointing at an uncommitted working tree cannot be resumed
cleanly — the next agent inherits a dirty tree with no record of what is
half-done. Commit small logical milestones as you work, so the checkpoint is a
pointer rather than a description.

## Frequency

- After each logical milestone.
- Before any risky or large change.
- When you notice context filling.
- Always before stopping.

Cheap to write, expensive to omit.
