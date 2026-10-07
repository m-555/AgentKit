# Skills and project guidance

AgentKit provides reusable workflow skills. A target repository provides its
own architecture, design and domain skills. MCP supplies tools and authoritative
job/task state; skills supply guidance. Installing MCP does not install native
skill discovery, and a skill is not a task queue or a permission grant.

## Canonical AgentKit skills

The single instruction source is `plugins/agentkit/skills/<name>/SKILL.md`.
Wheels include it under `agentkit/data/skills`; editable installations read the
source checkout. Native entries in this repo's `.agents/skills/agentkit-<name>`
and `.claude/skills/agentkit-<name>` point to the canonical source and do not copy
its body. These entries apply when working on AgentKit itself; `init` does not
copy the AgentKit source tree into target projects.

| Skill | Use |
|---|---|
| `migrate-project` | Onboard an existing repository and inspect its boundaries. |
| `plan-feature` | Define scoped tasks, interfaces and dependencies. |
| `separate-tasks` | Coordinate fresh builders and separate testers. |
| `decouple` | Split a demonstrated shared-file hotspot with behavior preserved. |
| `verify` | Interpret the assigned checks and integration evidence honestly. |
| `checkpoint` | Save concise state and continuation evidence. |

Choose applicable skills. Do not inject all six, the historical plans, or entire
architecture manuals into every worker. In separate-task mode builders do not
write or run tests; independent testers own tests and code runs combined gates.

## Provider-independent task delivery

A task's `skills` accepts a bundled name or an existing project-relative file:

```yaml
skills:
  - checkpoint
  - .agents/skills/project-design/SKILL.md
```

`agentkit.instructions.prompt` loads the role and each explicitly selected skill,
then checks the assembled context budget. `scheduler.launch` sends that prompt
through the chosen provider adapter. Codex and Claude both receive its text;
WSL Claude receives the same prompt through the host transport. Native skill
menus and a separately installed plugin are not required for this delivery.
Missing files, escaping paths and oversized context fail instead of being
silently dropped. Project paths resolve from the target project's root. Adding
a skill does not enlarge task write ownership or authorize another model.

For a compact task, the planner uses larger architecture/design references to
choose the seam, then assigns a short project worker guide and precise files.
Include the full domain skill only when the task actually needs it and the
context fits. Framework rules govern coordination; project rules govern domain
behavior, boundaries, file ceilings, CRLF and real checks. User choices govern
roles, providers, effort, review policy and pool sizes. Surface an unresolved
conflict rather than silently overwriting another project's rules.

## Native discovery is a separate route

Codex project skills in this workspace are catalogued from `.agents/skills`.
Claude Code documents `.claude/skills` for project skills and `skills/` inside an
enabled plugin for namespaced skills. A Windows plugin install does not prove
installation in the Linux user's Claude runtime. See [Claude's skill locations](https://code.claude.com/docs/en/skills#where-skills-live)
and [OpenAI's skills guidance](https://developers.openai.com/codex/skills/).

This source checkout includes native pointers for both clients. Its local Claude
marketplace remains optional for supervised workers; enabling it is an explicit
installation step. The filesystem entries are present, but an already-open
client may need its skill list refreshed or a new session to discover them.
Do not claim that installation or file presence proves a live model used a skill.

## Example: a target project

A target's architecture, design and orchestration skills remain canonical in its
own `.agents/skills`. Its `.claude/skills` entries point to those same files.
They are project-specific; no target jobs, models or skill bodies belong in AgentKit.
Compact tasks can select one short worker skill, such as `.ai/skills/worker.md`,
which is injected into both providers instead of preloading every longer project skill.
The native manager selects architecture/design guidance as needed and assigns
bounded worker context. Native project skill discovery is available in new client
sessions; explicit AgentKit task delivery works independently of that discovery.

## Maintain and check

Edit the canonical skill, not a native pointer. Keep name/description metadata
brief and align selected skills with the project's review and test policy.
Validate skill frontmatter and pointer targets, inspect an assembled task prompt,
and run only the checks affected by the change. Native pointers and task delivery
are not evidence of functional confinement, task quality or native chat wake.
