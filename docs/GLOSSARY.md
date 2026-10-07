# Glossary — every term in this repo, in plain English

For each term: **what it means in general**, then **→ what it does here** in AgentKit.
Read §A and §B first; the rest you can look up when you hit the word.

---

## A. The absolute basics

**Agent**
A program that uses an AI model to do work by itself — it reads files, runs commands and edits
code, instead of just chatting. Claude Code and Codex are agents.
→ Here: the workers that write your code.

**Session**
One running conversation with an agent, from when it starts to when it stops. Close it and it's gone.
→ Here: sessions are treated as **disposable**. All the important state lives in files, so losing
a session costs you nothing.

**Model**
The actual AI brain (Opus, Sonnet, Haiku, GPT-5). Bigger = smarter, slower, more expensive.
→ Here: each role gets a different one. Opus for planning, Sonnet for writing code, Haiku for
mechanical work. That's the main way costs are controlled.

**Token**
The unit text is measured and billed in — roughly ¾ of a word. 1,000 lines of code ≈ 10,000 tokens.
→ Here: why we don't load every document into every agent.

**Context window**
The agent's short-term memory — everything it can "see" right now: your instructions, the files
it read, the conversation so far. It has a hard size limit.
→ Here: **the scarce resource.** The whole framework exists because this fills up and gets lost.

**Compaction**
When the context window gets full, the agent summarises the old parts to make room. Detail is lost.
→ Here: the moment things go wrong — which is why a hook saves a checkpoint *before* it happens.

**Prompt**
The instructions you give the agent. A **system prompt** is the standing instruction it always
follows (its job description), as opposed to what you type each time.

---

## B. The seven building blocks

These are the things you actually create. All of them are just files.

**1. Instruction file** — `AGENTS.md` / `CLAUDE.md`
A document the agent reads *every single time*, automatically. Project facts: what the stack is,
where things live, house rules.
→ Here: `AGENTS.md` is the one source of truth. `CLAUDE.md` is a single line pointing at it, so
the two can never disagree.

**2. Subagent** — `.claude/agents/name.md`
A specialist worker with its own fresh memory, its own allowed tools, and its own model. Like
hiring a coworker who only does one job and only has keys to one room.
→ Here: your seven roles (architect, implementer, reviewer, integrator…). The `tools:` line is real
— a reviewer with no edit tool *cannot* edit, ever.

**3. Skill** — `.claude/skills/name/SKILL.md`
A how-to guide the agent picks up **only when it's relevant**. The agent always sees the title,
and only loads the full text when needed.
→ Here: procedures like "how to split a giant file safely". Saves context versus putting
everything in `AGENTS.md`.

**4. Slash command** — `.claude/commands/name.md`
A shortcut you type, like `/plan`. A saved prompt you can re-run with different inputs.
→ Here: `/plan`, `/work`, `/status`, `/handoff`, `/integrate`.

**5. Hook** — configured in `settings.json`
**A rule that runs automatically at a specific moment, and can say "no".** Not advice to the AI —
actual code that fires before or after the agent does something, and can block it.
→ Here: **the enforcement layer.** "Before any file edit, check the agent owns that file." The AI
doesn't get a vote.
*Analogy: a door that won't open, versus a sign asking you not to enter.*

**6. MCP server** — listed in `.mcp.json`
A small program that hands the agent extra tools (buttons it can press) and data. MCP is just the
standard way agents and tools talk to each other — the same server works with Claude *and* Codex.
→ Here: the **AgentKit server**, the spine of the whole system. It holds the task list, who owns
which files, and the save-files. Agents ask it things; they never edit that state directly.

**7. Plugin** — `.claude-plugin/plugin.json`
A bundle of everything above (agents + skills + commands + hooks + MCP), packaged so you can
install it with one command and update it with one command.
→ Here: **how the framework reaches all your projects.** Fix a bug once in the plugin, every
project gets the fix. Without this you'd copy files into 6 repos by hand and they'd drift apart.

**Marketplace**
The place plugins are installed from — like an app store. Can be a folder on your own disk.
→ Here: this repo *is* the marketplace. Add it from GitHub (`m-555/AgentKit`) or from a local clone.

---

## C. Safety and permissions

**Permission mode**
How much the agent may do without asking. `default` asks a lot; `acceptEdits` lets it edit freely;
`bypassPermissions` asks nothing.
→ Here: workers run in `acceptEdits` **because they're locked inside their own copy of the repo**.

**Allow / deny / ask rules**
A list of what's always fine, never fine, or needs a prompt — written as patterns like
`Edit(.env)` or `Bash(git push *)`.
→ Here: the second lock on the door. `.env` and database migrations are flatly denied.

**Sandbox**
A walled-off area where the agent can only touch the folder you gave it.
→ Here: one of seven enforcement layers. Which layers an agent actually has is **measured**
by a probe, never assumed — an early draft of this plan wrongly assumed Codex had no
pre-write hooks and built a whole design decision on it.

**Execpolicy / `.rules`** (Codex)
Codex's file listing which shell commands are pre-approved.
→ Here: the Codex-side equivalent of allow/deny rules. You already have one at
`~/.codex/rules/default.rules`.

---

## D. Git terms

**Branch**
A parallel version of your code you can change without affecting the main one.

**Worktree**
A **second folder on disk** holding the same repo at a different branch. Two agents in two
worktrees are editing genuinely different files, so they physically cannot overwrite each other.
→ Here: one worktree per agent. Non-negotiable — it's the foundation of safe parallelism.
*Analogy: two people photocopying a document and marking up their own copy, instead of fighting
over one sheet.*

**Integration branch**
A staging branch where finished work is combined and tested together *before* it reaches `main`.
→ Here: agents merge here; only you approve the final step to `main`.

**Merge / rebase**
Combining two branches. Rebase replays your changes on top of the latest code first, giving a
cleaner history.
→ Here: only the **integrator** role is allowed to do either.

**Diff**
The list of what changed. **Churn** = how often a file changes. **Fan-in** = how many other files
import it. **Co-change** = how often two files change in the same commit.
→ Here: churn and size predict *collisions* (two agents in one file); fan-in predicts *blast
radius* (how far a mistake spreads). They are scored separately, because a 400-line file that
everything imports and an 8,000-line file everyone edits are different problems.

---

## E. AgentKit's own vocabulary

**Orchestrator**
The program that runs the whole show: picks what to work on, starts agents, watches them.
→ Here: a small Python program (`agentkit`). The enforcement layers work without it.

**Scheduler** — decides *which* task runs next (what's unblocked, what won't collide).
**Supervisor** — watches running agents and notices when one dies, stalls or overspends.

**Task graph**
The list of jobs plus which must finish before which. "Add providers" can't start until
"create the provider interface" is done.
→ Here: lives in `.ai/tasks.yaml` (readable) and `tasks.db` (the live state).

**Task states**
`PLANNED` → `READY` (unblocked) → `LEASED` → `RUNNING` → `VERIFYING` (tests running) →
`REVIEW` → `INTEGRATION_READY` → `INTEGRATING` → `DONE`.
Plus `BLOCKED`, `FAILED`, `STALE` (worker lost), `NEEDS_REPLAN` (the task itself is wrong)
and `CANCELLED`. Only legal moves between these are allowed, and every move is logged.

**SAFE_PARALLEL / DEPENDENT / HOTSPOT**
The three kinds of work.
- *SAFE_PARALLEL* — separate files, run them all at once.
- *DEPENDENT* — must wait for something else.
- *HOTSPOT* — needs a file everyone else needs too, so **only one agent at a time**.

**Hotspot**
A file so central that every feature has to edit it. For example, an 8,000-line router that holds every endpoint.
*Analogy: a one-lane bridge. It doesn't matter how many trucks you have.*

**Ownership** — the recorded fact that task #101 is in charge of these specific files.
It lives on the task itself (in `tasks.yaml` and its lease), never in a separate file —
two places describing who owns what is how they end up disagreeing.
**Lease / lock** — that ownership as a temporary, expiring claim, like checking out a key.
→ Here: what the pre-edit hook checks. No lease, no edit.

**Checkpoint / handoff**
A save-file: what's done, what's left, which files changed, what to do next.
→ Here: written **automatically** by hooks when memory fills up or the agent stops — so a fresh
agent can pick up exactly where the last one died.
*Analogy: a save point in a game, not "remember to save".*

**Brief**
The starting packet handed to an agent: your goal, your files, your rules, your last checkpoint.
→ Here: injected automatically at session start, so you never paste a "continue where you left
off" message again.

**Heartbeat** — a periodic "still alive" signal, so a crashed agent gets noticed and replaced.

**Gate**
A test that must pass before work moves on — the project's tests, linter, type checker.
→ Here: declared once per project in `.ai/project.yaml`, which is what lets the same framework
work on Python, React *and* Unreal C++.

---

## F. Refactoring words

**Refactor** — change the *shape* of code without changing what it does.

**Seam** — a natural place to cut a big file, e.g. "all the image endpoints" vs "all the video endpoints".

**Decoupling** — separating tangled code so pieces can change independently.
→ Here: the prerequisite for parallel agents. One 8,000-line file means one agent, forever.

**Characterisation test**
A test that records what the code does *today* — not what it should do. If it still passes after
your refactor, you know you didn't break anything.
→ Here: the safety net before any hotspot split, and the rule is strict: **the tests must pass
without being edited.** Edited tests mean the behavior changed.

**Interface / contract / abstraction**
An agreed shape everyone codes against — "every video provider takes a prompt and returns a
`MediaResult`". Once it exists, five agents can build five providers independently.
→ Here: what makes parallelism possible at all.

**Registry pattern**
A lookup table where adding a feature means adding **one line** instead of editing shared logic.
→ Here: the highest-value refactor. Five agents adding five providers = five new files and five
one-line additions, instead of five agents fighting over one function.

---

## G. Command-line flags used in the plan

| Flag | Plain meaning |
|---|---|
| `-p` / headless | Run without a chat UI — for scripts |
| `--worktree` | Give this agent its own folder-copy of the repo |
| `--session-id` / `--resume` / `--fork-session` | Name a session / continue it / branch it into a copy |
| `--output-format stream-json` | Emit events as they happen, so a supervisor can watch |
| `--json-schema` / `--output-schema` | Force the answer into a fixed shape, so a program can read it instead of guessing from English |
| `--max-budget-usd` | Hard spending cap for one agent |
| `--effort` | How hard the model thinks (low → max) |
| `--add-dir` | Let the agent also touch this extra folder |
| `--model` | Which brain to use |
| `symlinkDirectories` | Share `node_modules` between worktrees instead of copying gigabytes |
| `sparsePaths` | Only check out the folders needed — faster on big repos |

---

## H. Files you'll see in a project

| File | What it's for |
|---|---|
| `AGENTS.md` | The always-read project brief. **The one you actually maintain** |
| `CLAUDE.md` | One line pointing at `AGENTS.md` |
| `.mcp.json` | "Here's the AgentKit tool server" |
| `.claude/settings.json` | Permissions, hooks, which plugins are on (shared, committed) |
| `.claude/settings.local.json` | Your personal overrides (not committed) |
| `.ai/project.yaml` | This project's stack, test commands, hot files, budgets |
| `.ai/architecture.md` | Module boundaries and contracts |
| `.ai/tasks.yaml` | The task graph, human-readable |
| `.ai/tasks.db` | Live task state (agents never edit this directly) |
| `.ai/runtime/task-101/handoff.json` | That task's save-file |

---

## One-paragraph version

You write a **plugin** once. It contains **subagents** (specialist workers), **skills** (how-to
guides), **commands** (shortcuts), and **hooks** (automatic rules that can block a bad action). It
talks to an **MCP server** that remembers the **task graph**, who **owns** which files, and the
**checkpoints**. Each agent works in its own **worktree** so they can't overwrite each other, and a
hook blocks any edit outside its **lease**. Before anything merges, **gates** (tests) must pass.
And because one 8,000-line **hotspot** file would force all of this back into single file, you
**decouple** it first — behind **characterisation tests** that prove nothing broke.
