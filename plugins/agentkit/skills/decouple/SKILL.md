---
name: decouple
description: Split a large, frequently-edited file into modules that separate agents can own, without changing behavior. Use when a file is a hotspot, when two tasks both need the same file, when planning parallel work on a monolithic module, or when asked to refactor for parallelism, break up a god file, or reduce merge conflicts.
---

# Decoupling a hotspot

A file that every feature must edit forces every agent to queue. Splitting it is
usually the highest-value work available, because it is what converts one worker
into several.

## Decide whether it is worth it

Run `hotspot_report`. It separates two different problems:

- **high collision** (churn x size) — agents land in the same file. Split it.
- **high fan-in** (many importers) — changes here break distant code. Stabilise
  its interface first, then split behind that interface.

A file that is merely *large* is not a hotspot. If nothing has touched it in three
months, leave it alone: you would be spending a refactor's risk to buy no
concurrency.

## The protocol

### 1. Safety net before anything moves

Characterisation tests must exist and pass first. They capture what the code does
**today** — bugs included — through the interface that will survive the split.

The test for a successful decouple is: *the characterisation tests still pass, and
you did not edit them*. Editing a test during a refactor means behavior changed.

### 2. Find the seam

Look at what the file actually contains, then cut along the grain:

| If the file is... | Cut by... | Each piece becomes |
|---|---|---|
| A router with many endpoints | endpoint group | `routes/<domain>/<group>.py` |
| A service handling several backends | backend | `providers/<name>.py` + a registry |
| A sequential pipeline | stage | `stages/<stage>.py` |
| A UI component with mixed concerns | logic vs rendering | a hook + subcomponents |
| A module with repeated `if kind == ...` | the dispatch | a registry table |

Prefer the cut that makes *future* features into new files. A router split where
adding an endpoint still means editing a shared aggregator is only half the win.

### 3. Move, do not rewrite

- Copy code across unchanged. Resist every improvement.
- Keep public names importable from the old location with a re-export, so nothing
  outside your scope has to change in the same commit.
- One seam per commit, each reviewable as "same code, new file".

### 4. Prove it

Run the characterisation tests unedited, then the `full` gate. Report the new
module list and which paths are now independently ownable.

## The registry seam, in detail

Worth calling out because it is the highest-leverage pattern here.

Before — every new provider edits a shared function:

```python
def generate(kind, prompt):
    if kind == "veo":     return _veo(prompt)
    elif kind == "ltx23": return _ltx23(prompt)
    # every new provider is an edit to this function
```

After — every new provider is a new file plus one line:

```python
# providers/base.py
class Provider(Protocol):
    def generate(self, prompt: str) -> MediaResult: ...

# providers/registry.py
REGISTRY: dict[str, Provider] = {}
def register(name, provider): REGISTRY[name] = provider

# providers/veo.py — one file, one agent, no shared edit
register("veo", VeoProvider())
```

Five agents can now add five providers concurrently, with a one-line conflict
surface each instead of a shared function each.

## Never

- Mix a decouple with a feature or a bug fix. It destroys reviewability.
- Split along file size rather than meaning. `video_part1.py` / `video_part2.py`
  gives you two hotspots.
- Change a public signature during the move. That is a separate, planned task.
