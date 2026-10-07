---
description: Make an existing repository work with AgentKit, including the refactoring it needs to support parallel agents.
---

Onboard this repository. Work through these in order and report at each step.

**1. Scaffold.** Run `agentkit init`. It writes `.ai/`, `AGENTS.md` and
`.claude/settings.json` without overwriting anything that already exists.

**2. Make the gates real.** The detected commands in `.ai/project.yaml` are a
guess. Actually run each one and confirm it passes on a clean tree. A gate that
does not work is worse than no gate — every agent will trip on it.

**3. Measure the hotspots.** Run `hotspot_report`. Record the top entries under
`hot_paths:` in `.ai/project.yaml`, and list anything widely imported under
`contracts:`.

**4. Fill in AGENTS.md.** Replace the TODOs. The structure map and the
"where do I do X" table are what stop agents guessing. Read the code to write them
— do not describe what you assume is there.

**5. Assess parallel readiness.** For each top hotspot, say whether work on it can
be parallelised today, and if not, what `DECOUPLE` task would fix that. Be concrete:
name the seams.

**6. Report.** Tell me:
- what was created versus left alone,
- which gates are real and which are still placeholders,
- the top hotspots, and the decoupling work needed before multiple agents are safe,
- the single highest-value first task.

Do not create tasks or change source code in this command. Onboarding is
measurement and documentation only.
