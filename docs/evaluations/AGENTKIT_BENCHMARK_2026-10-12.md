# AgentKit benchmark: solo and managed agents

Report date: October 12, 2026 (local time). Final counter snapshot: October 12, 2026, 02:02:08 local time.

## Observed result

On this small backend project, the single fresh Sol session was the only condition to produce a fully accepted solution. Both AgentKit conditions reached their original 45-minute active execution budget without an accepted project. These are observed pilot results, including framework setup and repair problems; they do not establish general model superiority.

## Project and protocol

The project was LeaseQueue: a standard-library Python SQLite queue and JSON command-line interface. It required strict validation, canonical JSON, idempotent enqueue, leases, retries, durable state, and atomic operation across independent processes. Each condition received the same frozen specification, empty source seed, and 40 external acceptance checks. Worker tests were separate from the external evaluator. The experiment ran outside the framework source repository.

| Condition | Models and responsibilities |
|---|---|
| Solo | One fresh GPT-6.1 Sol HIGH session implemented and tested the project |
| AgentKit Sol | Fresh GPT-6.1 Sol HIGH managers, builders, testers, and reviewers |
| AgentKit Qwen + Sol | Local Qwen backend builders; fresh Sol HIGH managers, testers, and reviewers |

Each condition allowed at most two external feedback rounds. AgentKit allowed up to two worker slots and one local Qwen inference at a time. Qwen used the normal easy/standard/complex task scale but was assigned backend implementation only. No Claude implementation arm was run.

## Results and time

| Condition | Accepted cases | Recorded outcome | Wall time | Active budget consumed |
|---|---:|---|---:|---:|
| Solo Sol | 40/40 | Accepted on first submission | 9.02 min | Finished before limit |
| AgentKit Sol | 0/40 recorded | Failed or incomplete | 45.12 min | 45.11 min |
| AgentKit Qwen + Sol | 0/40 recorded | Failed or incomplete | 261.31 min | 45.01 min |

The mixed run wall time includes a real Codex quota cooldown. Its tests and CLI were not completed and integrated by the cutoff. The Sol run completed five internal tasks and their reviews, but its external acceptance receipt recorded 40 Windows temporary-directory cleanup errors from retained SQLite database handles. Zero recorded successes therefore does not mean 40 independent behavioral assertions were proved wrong. The original receipt and evaluator were preserved.

## Token accounting

Input includes cached input. Output includes reasoning when the provider includes it. Total is input plus output: cached tokens and reasoning subsets are never added a second time. Missing measurements remain unknown. Subscription usage percentages and financial costs cannot be derived directly from these raw counts.

| Condition | Input | Output | Total | Cached input subset | Reasoning output subset |
|---|---:|---:|---:|---:|---:|
| Single Sol HIGH chat | 689,148 | 17,768 | 706,916 | 617,984 | 3,345 |
| AgentKit: Sol HIGH | 9,585,982 | 71,599 | 9,657,581 | 8,602,368 | 14,160 |
| AgentKit: Qwen + Sol HIGH | 8,564,896 | 107,104 | 8,672,000 | 7,837,691 | Unknown |

All participant owners had finished before this final counter snapshot. Totals include remaining owners finishing after the controller stopped at the budget cutoff; that later work does not change the failed/incomplete trial outcome.

Qwen thinking began disabled and was enabled externally at approximately 23:25 local time on October 11. The local router includes thinking in output but does not measure its token count separately. Later Qwen reasoning and visible-output subtotals are unknown; combined output and total tokens remain measurable. Different provider tokenizers and this condition change limit direct raw-token comparisons.

## Token use by role

| Role | Sol input | Sol output | Qwen + Sol input | Qwen + Sol output |
|---|---:|---:|---:|---:|
| Manager | 4,819,435 | 29,305 | 4,904,010 | 28,401 |
| Builders | 1,047,369 | 10,872 | 1,449,132 | 67,783 |
| Testers | 568,699 | 15,375 | 0 | 0 |
| Reviewers | 2,971,797 | 15,097 | 2,211,754 | 10,920 |
| Runtime probes | 178,682 | 950 | 0 | 0 |
| **Total** | **9,585,982** | **71,599** | **8,564,896** | **107,104** |

| Role | Sol sessions | Qwen + Sol sessions |
|---|---:|---:|
| Manager | 9 | 6 |
| Builders | 6 | 6 |
| Testers | 2 | 0 |
| Reviewers | 8 | 5 |
| Runtime probes | 3 | 0 |

These counts include retries. Managers consumed 50.2% of all Sol-run tokens and 56.9% of mixed-run tokens. In the mixed run, builder tokens came from Qwen; manager and reviewer tokens came from Sol. No mixed-run tester launched before the cutoff.

### What each role means

| Role | Responsibility in this experiment |
|---|---|
| Manager | Plans the task graph, assigns work, handles failures, and decides when the job is ready |
| Builder | Implements assigned source files and submits a scoped commit |
| Tester | Writes independent tests and runs declared checks after source dependencies are ready |
| Reviewer | Independently audits the exact submitted commit, scope, contracts, and required gates |
| Runtime probe | Performs a small diagnostic run to qualify provider execution and confinement, such as protected file-access checks |

Managers and reviewers used the same model, but ran in separate sessions with different responsibilities. Review tokens are not included again under managers. This experiment selected independent AI review; other review configurations are described in [Review workflows](../review-workflows.md). Builders did not approve their own commits.

Runtime probes are diagnostics rather than implementation work. The Sol run triggered three Codex probe sessions after execution had started, totaling 179,632 tokens. They are included in runtime costs. Probes before execution are reported separately as setup; zero mixed runtime-probe tokens does not mean it needed no setup probes. The supervisor, dashboard, and report watchers are model-free; their own execution consumes no model tokens.

## Efficiency and overhead

The primary efficiency measure was total tokens through full acceptance. Since neither AgentKit condition was accepted, neither qualifies for an accepted-solution efficiency ranking. The observed totals show substantial management and review overhead for this pilot. They identify where to investigate repeated context, repair loops, and control-session costs, but do not prove a particular optimization will preserve accuracy.

Participant totals exclude the parent chat that designed the experiment, audited infrastructure, and repaired the framework. Its separately recorded overhead snapshot was 16,375,414 tokens, including 15,697,920 cached input. That snapshot is an ongoing lower bound, not a final overhead total. Failed initial setup, pre-execution probes, and these design/repair costs belong in a separate cold-adoption view rather than silently disappearing.

## Reliability and automatic recovery

| Evidence | Result | What it establishes |
|---|---|---|
| Frozen external acceptance | Solo 40/40; both AgentKit conditions unaccepted | Outcome on this project and budget |
| Accelerated cooldown recovery | 10 checks passed | Real recovery APIs exercised with simulated five-hour timing and task delivery |
| Availability/dispatch matrix | 13 checks passed | Model-free policy cases for Codex/Claude availability, assignments, complexity, Qwen backend roles, and guards |
| Natural Codex quota reset | Observed, followed by automatic continuation and task completion | Recovery of dependent work under a real account cooldown |
| Controlled five-hour outage | Not qualified | No claim of an actual imposed five-hour outage or full-repository completion |
| Token accounting regression checks | 7 passed | Subsets, counter resets, deduplication, and unmeasured local reasoning |

The real cooldown was detected at 21:42 local time on October 11. Availability returned at 01:18 on October 12; the supervisor automatically launched a reviewer and coordinator at 01:18:30. After automatic correction and PASS review, mixed task 1 became DONE at 01:27:49. No root implementation edit was needed for this continuation. Qwen SQLite work then required repeated review corrections, including a concurrent-startup issue.

AgentKit demonstrated automatic quota recovery and retry scheduling here. End-to-end completion with minimal intervention was not established: earlier framework repairs needed manager intervention, and both managed trials ended without an accepted project. Claude dispatch was checked through policy fixtures, not measured through a real Claude project run.

## Framework changes during the pilot

| Change | Commit | Recorded verification |
|---|---|---|
| Bounded coordinator approvals, Windows confinement evidence parsing, local input/cache normalization | `61fdd0b` | 4 failures characterized; 105 related checks; full suite 1,860 passed, 8 skipped |
| Ignore discovery provenance only when executable and guards still match | `21176ff` | 2 failures characterized; 55 related checks passed |
| Scoped reviewer tools and post-gate review identity validation | `85d3190` | 3 failures characterized; 104 passed, 1 skipped |
| Expose declared contracts in briefs and reviewer guidance | `ea65274` | 2 failures characterized; 32 related checks passed |

These were universal framework changes. The framework changed during the Sol trial, and the mixed trial inherited those repairs. Another manager also advanced the shared installed source during the experiment; the continuation recorded commit `4fbd822` and pending changes. The framework was therefore not fixed across conditions. The later documentation snapshot does not retroactively certify a newer checkout.

## Limits and next experiment

- One project and one run per condition cannot establish general reliability or error rates.
- Framework and Qwen thinking changes confound attribution to the providers.
- Internal gate/review success did not guarantee external acceptance.
- Raw tokens include cache hits, use different tokenizers, and are not monetary cost or allowance percentages.
- Real reset recovery completed one task; it did not establish an entire repository finishing automatically.
- A follow-up should pin the same framework commit, freeze the thinking setting, make resource-lifetime expectations explicit before freezing acceptance, and repeat at least three counterbalanced trials per condition on equivalent fresh projects.

## Evidence and reproduction

Raw artifacts are retained outside the AgentKit repository on the original machine. They are provenance, not a requirement or runtime dependency for AgentKit.

| Artifact | Contents |
|---|---|
| `SPEC.md`, `PROTOCOL.md`, `manifest.json` | Frozen specification, experimental rules, hashes, and framework phases |
| `REPORT.md`, `results.json` | Machine-generated overview and detailed metrics |
| `runs/*/tokens.json` | Session counters, model requests, normalized totals, and coverage |
| `runs/*/acceptance-*.json`, `arm-result.json` | Original evaluation and cutoff outcomes |
| `controller-events.jsonl`, `condition-phases.json` | Chronological events and changed experimental conditions |
| `continuity/live-observations.jsonl`, `continuity/qualification.json` | Natural cooldown, resumed processes, and completed-task receipts |
| `counter-drain-state.json` | Confirmation that remaining owners finished and final counters were published |

The original failed setup under `runs/agentkit-sol` remains separate from `runs/agentkit-sol-qualified`. Frozen evaluator files were not rewritten to make the managed conditions pass.
