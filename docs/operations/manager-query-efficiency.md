# Small manager reads

Use `job_brief(job_id)` for current intent and unfinished work. The default
response retains every user request and acceptance criterion, plus counts,
a page of twenty unfinished tasks, current manager registration and the
recovery epoch. It excludes nested audit, launch and checkpoint histories.

Use `offset` and `limit` to page unfinished tasks; `limit` accepts 1 through 50.
Decisions and blockers have short operational previews with explicit original
and omitted character counts. Those previews do not replace saved evidence.
`job_brief(job_id, include_history=True)` retains the original complete report
for exceptional investigations. Do not request that report repeatedly for
routine polling. `job_decision` now returns only its saved receipt: job id,
revision and decision count.

Workers still receive the complete user intent through their bounded job
context and their own exact task brief. This change does not truncate worker
instructions, weaken review or change task authority.

An already running MCP server retains its loaded Python code. New servers use
the updated default; reconnect an idle editor MCP connection to refresh its
schema. Do not restart a working AI session just to refresh a display.

# Live CLI screens

Only STARTING or RUNNING process records can appear in the live-screen section.
Recorded process birth identities are included in the internal liveness query
before classification, so a reused process number cannot revive an old run.
Completed records with uncertain ownership remain visible in the attention or
history views; no unrelated operating-system process is killed.

The monitor stays collapsed until an actual registered window frame is
available. An unavailable window produces a clear message, not an empty
black screen or a simulated terminal. The VS Code manager chat can have no
CLI window; its messages remain in that chat. Interactive keyboard input is
still restricted to a verified native CLI manager registration.

User-enabled worker viewers explicitly request a visible console and record
startup failures in `.ai/runtime/process-N/viewer.stderr.log`. They never own,
stop or resume the worker. A viewer closes when the owning session stops.

Dependency preparation fingerprints the base Python interpreter, rather than the host virtualenv wrapper. Identical host tooling therefore reuses prepared project dependencies. Manifest, runtime and profile changes still invalidate readiness. A live preview must release dependency file locks before an intentional dependency refresh.

An operator may queue an unchanged, approved integration retry during recovery. This repairs the failed-task audit finding; it does not launch a worker or waive recovery acknowledgement/full integration checks. Read-only research receives a separate prompt with no commit/edit/test instructions. Local Qwen remains read-only until write isolation is qualified.


## Host preparation and explicit repair

A launch packet uses current required inputs and separate output deliverables.
Successful host setup records a fresh mechanical checkpoint before recovery text
is assembled. Historical setup failures remain in prior checkpoints.

A new launch claim has at most 60 seconds to record its monitor PID. During that
bounded interval reconciliation reports a settling launch, not a live worker.
An exited, older or superseded claim receives normal stale handling.

An authenticated planner's fresh retry cancels obsolete recovery claims only
after the prior monitor and child are stopped. Their proofs and journal remain.
A normal READY task cannot be needlessly requeued. Exact stopped STALE files may
be committed by host completion after ownership, fingerprint, scope and gates;
workers cannot grant themselves this recovery transition.

For a failed combined gate on an unchanged approved commit, integration_retry
can name independent TEST_ONLY remediation tasks with after_tasks. Source remains
FAILED and unmerged while those tests are unfinished. A recovery audit recognizes
this explicit hold only while exact source approval and valid same-job testers
remain intact. After all named tests are DONE, code queues the unchanged source
for normal full gates. It launches no replacement builder and grants no merge
approval. Failed, dependent, cancelled, foreign or changed remediation remains
blocked.

## Preserve an independent failing test

For a stopped READY TEST_ONLY task with an exact clean recorded commit, the
attached manager may call test_hold(task_id, source_task_id, evidence). The
source correction must be an explicit same-job dependency. This atomically
blocks a new model launch; live or uncertain owners, dirty drafts, changed
commits and unrelated corrections are refused. No branch is deleted.

After that source correction is accepted, test_refresh carries byte-identical
tests onto integration, archives the prior head and runs the declared gate.
Independent review and the full integration gate still apply. A failing
assertion is retained rather than rewritten by another paid session.
