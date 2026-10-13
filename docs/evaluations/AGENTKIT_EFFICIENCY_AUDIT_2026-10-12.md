# AgentKit efficiency findings and proposed plan

Date: October 12, 2026. This is a research report and implementation plan.
Another agent owns implementation. Findings describe the recorded pilot and
source inspected during the audit; they do not certify the current working tree.

## Conclusion

The managed workflow failed the efficiency objective on this small backend
project. Sol consumed 13.7 times the solo run's tokens; Qwen plus Sol consumed
12.3 times. Neither managed condition produced an accepted project within its
45-minute active budget. Most tokens went to coordination and review.

The central issue is excessive model turns combined with repeated context.
More agents do not inherently reduce tokens or elapsed time. A small task needs
a small execution route; multiple workers help when useful work can proceed
independently or when isolation avoids sending irrelevant context repeatedly.

## Measured results

See the [original benchmark report](AGENTKIT_BENCHMARK_2026-10-12.md) for the
protocol, final role tables, counters, framework changes, and acceptance caveats.

| Condition | Input tokens | Output tokens | Total tokens | Accepted | Wall time |
|---|---:|---:|---:|---|---:|
| Solo Sol HIGH | 689,148 | 17,768 | 706,916 | 40/40 | 9.02 min |
| AgentKit Sol HIGH | 9,585,982 | 71,599 | 9,657,581 | No | 45.12 min |
| AgentKit Qwen + Sol HIGH | 8,564,896 | 107,104 | 8,672,000 | No | 261.31 min |

The mixed wall time includes a natural Codex quota cooldown; active time was
45.01 minutes. Sol used 45.11 active minutes. Final counters include remaining
owners finishing after the cutoff. Input includes cache reads, and output
includes thinking where supplied; neither subset is added again to totals.

| Trace measurement | AgentKit Sol | AgentKit Qwen + Sol |
|---|---:|---:|
| Manager sessions | 9 | 6 |
| Independent reviewer sessions | 8 | 5 |
| Manager plus reviewer share of all tokens | 81.1% | 82.5% |
| Codex model responses | 269 | 152 |
| Local OpenCode step-finish receipts | 0 | 73 |
| Job-brief calls | 27 | 25 |
| Calls explicitly requesting full history | 26 | 19 |
| Total job-brief text returned, characters | 1,259,425 | 783,827 |
| Largest job-brief text response, characters | 125,014 | 50,492 |
| Largest manager-audit text response, characters | 111,251 | 67,831 |
| Failed completed MCP calls | 22 | 16 |

The solo session produced 21 recorded Codex model responses. The Sol managed
count includes 108 coordinator responses, 81 review responses, 69 worker
responses, and 11 runtime-probe responses. Local step-finish receipts are a
different measurement and are not treated as identical to Codex model responses.
Character counts are actual text lengths, not token estimates or proof that every
returned character reached the provider identically. Serialized MCP events also
contain structured representations; those are not counted twice in this table.

## Causes: observations and implications

| Finding | Evidence in the pilot/source | Implication |
|---|---|---|
| Full operational history was routinely requested | Most job-brief calls used include_history=True; some returned more than 100,000 text characters | History is repeatedly added to contexts and processed in later turns |
| Compact reads lacked required policy metadata | At inspection, compact_jobs.summary omitted model profiles, assignment pins, backend-only restrictions, and durable review/model selections that control agents needed | Missing metadata gives an agent a reason to choose full history; that behavioral explanation is an inference supported by the call trace |
| Managers repeatedly reconstructed state | Nine fresh Sol coordinator sessions; workflow.resume_token discards resume tokens when separate-tasks is enabled | The persistent coordinator role loses conversation continuity; blindly resuming a long conversation could also increase cost without context management |
| Small work was over-decomposed | Three dependent implementation modules plus separate test tasks and per-task reviews | Little opportunity for parallel speed gains, but many handoffs and model sessions |
| Controls performed extensive inspection | Managers and reviewers account for most tokens; large job/audit reads accompany repeated source reads | Task-scoped evidence and changes should replace routine global reinspection |
| Tooling failures caused additional loops | Failed MCP calls and recorded approval/confinement/reviewer-access repairs | Runtime problems need infrastructure handling rather than unnecessary source rebuilds |
| Compact defaults could still receive oversized intent | Sol evaluator feedback placed about 79,056 characters of repeated failures in the authoritative requests | Operational compression alone cannot remove this; deduplicate feedback before constructing future amendments and retain full receipts outside prompts |
| Control tools remained broad | Current inspected MCP surface listed 45 tools for controls versus 11 for workers | Role-specific tool surfaces may reduce irrelevant choices and context; the exact token saving has not been measured |

Input was 99.3% of Sol-run tokens. Sol reasoning was 14,160 tokens, about 0.15%
of all tokens. Lowering thinking effort alone cannot address the dominant input
and turn-count overhead. Runtime probes used 179,632 tokens, about 1.9% of the
Sol total; they were not the principal source of inefficiency.

These findings separate observed behavior from architectural hypotheses. They
do not assign every extra token to one cause or promise a saving from one patch.

## Practices in established agent projects

| Project/source | Relevant practice | Application to AgentKit |
|---|---|---|
| [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) | A minimal coding loop, bash-based actions, and straightforward message history | Keep a capable small-task route instead of requiring a large agent hierarchy |
| [LangChain subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents) | Control subagent inputs and return concise results; isolate intermediate work | Send task-relevant evidence and short receipts to the manager |
| [Aider repository maps](https://aider.chat/docs/repomap.html) | Select relevant repository information within a token budget | Load relevant code/context rather than repeated broad repository descriptions |
| [OpenHands context management](https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents) | Condense growing history while retaining goals, important files, and failures | Bound useful history and preserve durable evidence separately |
| [Anthropic workflow guidance](https://www.anthropic.com/engineering/building-effective-agents) | Start with the simplest sufficient solution and evaluate latency/cost tradeoffs before adding orchestration | Select agent count and workflow complexity according to useful work, rather than configured slots |

These are design references, not head-to-head performance results on LeaseQueue.
The frameworks' published results cannot establish AgentKit's future savings.

## Proposed implementation plan

All rows below are proposals. The implementing agent must reconcile them with
current fixes and preserve configured authority and acceptance requirements.

| Priority | Work | Expected mechanism | Verification |
|---|---|---|---|
| P0 | Make compact control packets sufficient: include model pins, assignment restrictions, required acceptance, review mode, current recovery digest, and actionable findings | Remove the need to fetch full operational history for routine decisions | Compare compact/full responses; verify exact intent and policy remain available; characterize old failures |
| P0 | Classify infrastructure failures separately from implementation findings; deduplicate diagnostic amendments with references to retained receipts | Stop unnecessary builder/reviewer loops and repeated traceback replay | Inject denied tools and unavailable runtimes; preserve code, failed tests, ownership, and honest blocked status |
| P1 | Add or select an explicit lightweight route for small, tightly coupled jobs | Reduce task splitting, discovery, and control sessions | Compare against existing separate-tasks mode on the same acceptance suite; disclose any changed review/test policy |
| P1 | Keep routine scheduling, dependency release, availability checks, gates, and reset wake in deterministic code; use AI for meaningful decisions | Reduce manager round trips between predictable events | Verify routine successful transitions need no extra planner session and changed scope still triggers a decision |
| P1 | Provide reviewer packets with the exact diff, relevant contracts, and valid gate receipts; consider configured combined review for small jobs | Reduce duplicated inspection and gate execution | Review exact commit sets independently; reject stale receipts and preserve stronger workflows for risky work |
| P1 | Use role-specific tool surfaces and bounded operational outputs with paging | Reduce unnecessary tool choices and oversized responses | Measure advertised schema size, denied-call rate, and ordinary response sizes; preserve required recovery operations |
| P1 | Define a bounded coordinator memory policy: concise current state, meaningful deltas, and explicit detail retrieval | Avoid both fresh-session rediscovery and unlimited resumed history | Test restart, quota reset, changed requests, unchanged blockers, and correct retrieval of detailed evidence |
| P2 | Add total job input/output budgets and role-level usage attribution, including retries | Prevent uncontrolled spending and expose where the budget goes | Retain partial work on budget hold; keep counters provisional until owners finish; never claim completion from a budget stop |
| P2 | Repeat a controlled benchmark after freezing the framework and experimental settings | Establish whether changes actually improve accepted-solution efficiency | At least three equivalent fresh trials per condition, with acceptance, time, counters, and interventions recorded |

The lightweight route is an explicit policy choice, not permission to silently
remove independent review, alter model pins, lower requested effort, or assign
frontend work to a backend-only Qwen assignment. Other projects may keep stronger
separation and auditing. Parallelism should follow independent work and ownership.

Several proposed mechanisms already exist in part: compact reads, short worker
summaries, worker tool filtering, event-based planning, and deterministic recovery.
The task is to make the execution path use them consistently, and to measure
whether they reduce work rather than merely adding another layer.

## Acceptance targets for the next experiment

These are engineering targets for the mini-project, not achieved results or
promises that every repository will use fewer tokens than solo.

| Metric | Target |
|---|---|
| Functional acceptance | All 40 external checks pass; preserve meaningful behavioral coverage |
| Total warm execution tokens | Below the observed solo total of 706,916, including managers, workers, reviewers, retries, and runtime probes |
| Speed without provider cooldown | Below the observed solo 541.4 seconds through acceptance; report real wall time and quota wait separately |
| Control overhead | Target less than 20% of all tokens; report actual share and quality tradeoffs |
| Reproducibility | Same fixed framework commit, comparable model settings, frozen thinking condition, equivalent fresh tasks, and repeated trials |
| Recovery | Preserve automatic reset wake and verify resumed completed work, not just a timer firing |

Report setup/probe costs and human/root-manager intervention separately, without
moving required implementation or review into an excluded parent chat to create
an artificial saving. Report cached/uncached input and cloud/local subtotals.
Different tokenizers and cached-input pricing prevent raw totals from serving as
a dollar-cost or subscription-allowance calculation.

## Benchmark caveats and evidence

The pilot changed framework versions during execution, and Qwen thinking changed
mid-run. Both managed conditions were unaccepted. Sol's external receipt recorded
40 Windows temporary-directory cleanup errors from retained SQLite handles;
these are not proof of 40 separate behavioral assertion failures. A follow-up
must make resource-lifetime expectations explicit and freeze the scorer before
running, rather than rewriting the historical result to manufacture a pass.

Natural Codex quota recovery and completion of one dependent task were observed.
Complete repository delivery after recovery was not established. No real Claude
implementation trial was included. One pilot cannot establish general error rates.

Raw evidence is retained outside the framework source checkout,
on the original machine:

- results.json and runs/*/tokens.json: final counters and session attribution.
- efficiency-audit.json: model-response counts, full-history flags, tool text
  lengths, failed MCP calls, and an offline packet-size comparison.
- controller-events.jsonl and continuity/qualification.json: cutoff and natural
  quota recovery evidence.
- condition-phases.json and manifest.json: changing model/framework conditions
  and frozen artifact hashes.

An earlier uncommitted experimental metadata/prompt patch was stopped at the
user's instruction. An offline comparison measured 13,561 compact characters
versus 49,298 full-history characters, a 72.5% response-size reduction. This is
not a measured runtime token saving, a new benchmark, or implementation approval.
The implementation agent owns decisions about that patch and the remaining work.

## Implementation status (added by the implementing agent, October 12, 2026)

The trace figures above were re-checked against the rollouts: 26 of 27 job_brief
calls requested full history (managers and reviewers), and the response counts by
role match. The largest job_brief response found in the Sol rollouts was 48,164
characters; the 125,014 figure could not be reproduced from them.

| Proposal | Status |
|---|---|
| P0 compact control packets | Done: the uncommitted patch was reviewed and adopted (`efa7054`). Fresh managers are told the compact read is complete for planning (`998e138`); resumed managers read a bounded digest (`0abf9bc`) |
| P0 infrastructure failures | Done (`0bf292e`): reviewer BLOCKED verdict, worker `infrastructure=true`, and unchanged resubmission stop the job until the operator retries |
| P0 deduplicated diagnostics | Done in two places: a user update that would push the job's intent past `max_context_chars` is refused with guidance to use a named file (`998e138`); the re-benchmark harness sends distinct errors only (63,319 to 520 characters on its first receipt) |
| P1 lightweight route | Done as `workflow.mode: feature`, the default for new projects (`685b9c2`); a job that is one coherent change is planned as one task (`998e138`) |
| P1 deterministic routine work | Done for review findings (`0abf9bc`: CHANGES requeue the builder without a manager session); scheduling, release, gates and reset wake were already code |
| P1 reviewer packets | Done (`998e138`): exact diff (paged past 15,000 characters), scope audit, gate receipt at the commit. Combined manager/reviewer review was not adopted: an independent clean-context review is kept |
| P1 role-specific tool surfaces | Not adopted: first-turn input was 16,957 tokens for the 45-tool manager against about 16,400 for 11-tool workers, about 550 tokens; Codex code mode lists tools on demand and Claude defers MCP tools. Lean launch flags (`ad202a9`) cut about 1,500 tokens per request instead |
| P1 bounded coordinator memory | Done (`0abf9bc`, `998e138`): the manager resumes its session with a digest, and starts fresh once its last session read more than 600,000 input tokens |
| P2 job budgets and attribution | Done as optional `budgets.job_tokens` (`998e138`), also in the view; per-role usage was already in the view |
| P2 controlled re-benchmark | Prepared, not run: pinned non-editable framework install, three trials per arm, SPEC v2 with the resource-lifetime rule. A first solo run under SPEC v1 also failed 40/40 on the retained-handle cleanup error, confirming the rule must be explicit before results can compare |

Brief size is capped at 4,000 characters by default (`ad202a9`) for workers and
reviewers; full briefs remain available with `full=true`.
