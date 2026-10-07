---
name: separate-tasks
description: Plan and coordinate short fresh-session implementation tasks with separate test-only agents, configurable per-project managers/models, and measured session visibility.
---

# Separate builders and testers

Read the project's model policy and user instructions first. Do not assume Codex
or Claude is always manager/reviewer. The user can choose providers, model IDs,
effort and pool counts for each project. A common team is one manager/reviewer
plus two builders and two testers for backend and frontend (nine slots).

The source checkout also documents configuration in docs/efficient-workflow.md.
Enable workflow.mode=separate-tasks only after replanning incompatible tasks.
Use exact owned files, short read context, one result and explicit dependencies.
Defaults: three write files, six read files, 1,200 assignment characters,
16,000 assembled context characters. Oversized authority requires replanning,
never truncation. Workers stop at configured turn/tool/time/reported-output caps;
inspect preserved checkpoints instead of automatically repeating failed tasks.
Builders own source; testers own tests and run focused checks. The planner also manages and assigns work. Choose workflow.review=human for user
feature approval, or ai for independent AI review; builders never approve themselves.
Human mode uses mechanical staging, not product approval, and requires independent
testers before final user acceptance.

Assign through AgentKit MCP definitions and its scheduler, not native subagent
spawning or custom CLI runners outside recorded ownership.
Start fresh sessions per task and retry. Preserve checked work and concise
checkpoints before continuation; prove the old owner stopped. Use approved model
pins and measured runtime guards. Never bypass limits or silently switch models.
Wait between task events instead of repeatedly reading files or pinging models.
Do not run duplicate full suites after stable passing validation.

The localhost dashboard shows recorded sessions, roles, tasks and token counters.
Team choices can be exported or applied to an idle project with operator controls;
neither route starts jobs.
Unknown usage remains unknown. Do not claim warm caches, thinking totals, quota
recovery or efficiency gains without observed provider evidence.

Qwen is optional for future easy tasks only after quality and isolation qualification. Honor each project's local-model exclusions; router tests do not prove test-writing quality.

Code owns routine transitions: declare a tester's same-job, same-area builder
prerequisites and exact source read scope once. The scheduler releases completed
dependencies, assembles tester packets, checks versions and runs declared gates.
Do not rewrite the handoff in another planning turn. Worker pool caps are enforced
when workflow.worker_slots is configured. Prefer events over model-based pings.

Human review binds notes and approval to the current job revision, preview commit
and verification digest. Changes invalidate that decision. Never impersonate a
user through job_accept or a worker session. The dashboard is read-only by default;
--operator-controls enables narrow local human decisions and idle-project profile
changes, without starting jobs. Choose a profile before submitting a new job.
