# Historical sandbox review

The implementation now treats static sandbox settings as unverified. Functional
probing requires a successful in-scope control write and tool-level evidence of
an outside-workspace denial. Unverified installations cannot run unattended
write tasks. The notes below preserve the original finding; their description
of current probe behavior is superseded by this fix and the README.

Found while piloting on a private repository (Windows 10, native). Left unfixed on purpose:
every available fix trades away something the project explicitly promises, so the
choice is yours.

---

## What the code does today

`probe._claude_sandbox_enabled()` ([probe.py:167](../../orchestrator/agentkit/probe.py#L167))
answers "is this agent's filesystem confined?" by **reading a settings file**:

```python
for source in (Path("~/.claude/settings.json"), Path("~/.claude/managed-settings.json")):
    sandbox = data.get("sandbox")
    if isinstance(sandbox, dict) and sandbox.get("enabled") is True:
        return True, f"sandbox.enabled is true in {source.name}"
```

That value becomes `workspace_sandbox`, which derives `write_worker_safe`, which
the scheduler requires before assigning **any** task that writes source
([capabilities.py:44](../../orchestrator/agentkit/capabilities.py#L44)).

## Why that is wrong on this machine

Per Anthropic's own documentation, the Claude Code sandbox
**runs on macOS, Linux and WSL2 only — native Windows is not supported.** If the
sandbox cannot start, Claude Code prints a warning and *runs commands
unsandboxed*.

So on Windows:

| | |
|---|---|
| User sets `sandbox.enabled: true` | AgentKit reports `workspace_sandbox: true` |
| Actual OS confinement | **none** |
| Scheduler behaviour | hands out unattended write work |

The probe cannot tell the difference, because it never asks the OS anything.

## Why this matters more than a normal bug

It contradicts the project's own stated invariant, in the same file:

> *Invariant 8: capabilities are measured, never assumed. Nothing in this module
> keys off a version string.*

and the function's own docstring:

> *Reporting `true` because the schema has a sandbox key would hand unattended
> write work to an unconfined agent — exactly the cross-worktree hole §2 exists
> to close.*

Reading a settings file is not measurement. It is the same category of mistake the
docstring warns against, one level further in: instead of trusting a *schema*, it
trusts a *user's intention*.

And the hole it guards is the one failure no later layer can catch. From
[capabilities.py:38](../../orchestrator/agentkit/capabilities.py#L38): if worker A writes
into worker B's worktree, B's audit sees a change to a path B legitimately owns,
and A's branch does not contain it — so no diff, at any layer, can attribute it.
L5, L6 and L7 are all blind to it by construction. The sandbox is the only thing
standing there.

**Severity depends entirely on worker count.** The cross-worktree hole requires two
concurrent workers to exist. At `--max-workers 1` there is no second worktree to
corrupt, and the remaining layers (L3 pre-write hook, L4 shell guard, L5 audit,
L6 pre-commit, L7 merge gate) all still hold — I verified L3 and L7 working on
Windows during the pilot.

---

## The options

### A. Measure it instead of reading it
Write a file outside the workspace from inside the agent and see whether it lands.
That is what the flag claims, so that is what should be tested.

- **For:** the only option that satisfies invariant 8. Correct on every platform,
  including ones not yet considered.
- **Against:** belongs in `probe --functional` (costs a real agent launch and
  tokens). The static probe would have to report `unknown` rather than `false`,
  which means a third state and a scheduler that understands it.

### B. Platform gate
Treat `sandbox.enabled` as false on `sys.platform == "win32"` unless running under
WSL, since the documentation says it cannot work there.

- **For:** small, honest, fixes today's wrong answer.
- **Against:** still assumption, not measurement — just a better-informed one.
  It hardcodes a vendor's current platform matrix in core, which is precisely
  what the adapter boundary exists to prevent. Goes stale silently when that
  changes.

### C. Let the operator accept the risk explicitly
Add something like `allow_unconfined_writes: true` to `.ai/project.yaml`, with the
scheduler honouring it only up to `--max-workers 1`.

- **For:** matches how the pilot actually has to run on Windows. Makes the trade
  visible in a committed file and bounded to where it is genuinely safe.
- **Against:** a documented way to turn off the guarantee. Anyone who sets it and
  later raises `--max-workers` gets the hole back silently — unless the cap is
  enforced in code, which it would have to be.

### D. Leave it, document it
Windows users run Codex, or run Claude in WSL2.

- **For:** zero risk of weakening anything; the refusal is correct today.
- **Against:** the misleading `true` is still reachable by anyone who sets the
  flag, and nothing warns them. Doing nothing leaves the trap armed.

---

## What I would do

**B + C together, and D is not acceptable on its own.**

B makes the answer honest today: on native Windows the probe should report `false`
with a note naming the reason, so setting the flag can no longer manufacture a
`true`. That closes the trap.

C then gives you a real way to work, instead of an accurate "no" you have to route
around by hand — which is what I was reduced to during the pilot, and what my own
harness correctly blocked me from doing. Bounded to one worker, it gives up nothing
that is actually being protected, because with one worker there is nothing to
protect *from*.

A is the right long-term answer and belongs on the `probe --functional` roadmap,
which the README already flags as unfinished. It should not block B and C.

Whatever you pick, the note attached to the capability should say **why** the
answer is what it is — that is what made this diagnosable at all.

---

*Nothing in this file has been implemented. Eight other pilot defects are fixed in
`fix/pilot-bugs`; this one was held back for your decision.*
