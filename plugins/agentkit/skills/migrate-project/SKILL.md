---
name: migrate-project
description: Bring an existing repository under AgentKit, including assessing whether it needs refactoring before multiple agents are safe on it. Use when onboarding a project, setting up multi-agent work on an existing codebase, or asked to make a repo agent-ready.
---

# Onboarding an existing repository

Most repositories are not ready for concurrent agents. Onboarding is two jobs:
wiring up the framework, and finding out how much refactoring stands between the
repo and safe parallelism. Do not skip the second.

## 1. Scaffold

`agentkit init` writes `.ai/project.yaml`, `.ai/ownership.yaml`,
`.ai/architecture.md`, `AGENTS.md`, `CLAUDE.md` and `.claude/settings.json`.
It never overwrites an existing file. A repo with a good `AGENTS.md` keeps it.

## 2. Make the gates real

Detected commands are guesses. **Run each one.** A gate that fails on a clean tree
will make every agent believe it broke something. If a project has no usable test
command, say so plainly — AgentKit's review and merge steps are worth much less
without one, and that is a finding worth reporting rather than papering over.

## 3. Consolidate instruction files

Repos accumulate `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules` and they
drift apart. Pick `AGENTS.md` as canonical — Codex, Claude and most others read it —
and reduce the rest to a one-line import. One file to maintain, no contradictions.

## 4. Measure before promising parallelism

Run `hotspot_report`. Then read the top files and judge honestly:

- **Can two agents work here today?** Only if they can be given disjoint files.
- **One file with everything in it?** Parallelism is impossible until it is split.
  Say so; do not plan around it.

Record the top entries under `hot_paths:` and anything widely imported under
`contracts:` in `.ai/project.yaml`. Those become the paths that require an explicit
lease.

## 5. Write AGENTS.md properly

The template has TODOs. Fill them by reading the code, not by assuming:

- **What this project is** — a few sentences.
- **Structure** — one line per significant directory.
- **Where do I do X** — a table mapping common tasks to locations. This is the
  highest-value section: it is what stops five agents putting the same thing in
  five different places.
- **House rules** — conventions that are real in this codebase.

Nested `AGENTS.md` files in large subtrees beat one enormous root file.

## 6. Report

- What was created versus left alone.
- Which gates are real; which are placeholders.
- Top hotspots, and the decoupling needed before multiple agents are safe.
- The single highest-value first task.

## Do not

- Change source code during onboarding. Measurement and documentation only.
- Declare a repo parallel-ready because the framework installed cleanly. Those are
  unrelated facts.
