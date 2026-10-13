# When something stops: problems, replies and your phone

A job is written once, in your own words, in the view's **Jobs** tab, with
screenshots when they help. The assigner turns it into tasks. Builders build, and
each task has to pass its own tests. A review follows, by task or once per job
(see [Review workflows](review-workflows.md)). Code accepts the finished job.

After planning, no model is woken to decide what a stop means. A stop becomes a
**problem**: code records what happened and which replies can fix it. Tasks that
do not depend on the stopped one keep running and merging.

## What becomes a problem

| Problem | Example | Replies |
|---|---|---|
| setup | A tool, permission or guard was denied; a reviewer could not run | retry, hint*, skip, plan |
| failed | A builder used up its automatic retries (failed gate or crash) | retry, hint, skip, plan |
| review | The review still asks for changes after 3 rounds, or rejects the approach | accept, hint, skip, plan |
| merge | An approved commit could not merge, or the combined checks failed | retry*, hint, skip, plan |
| replan | A builder says the task cannot be done as planned | plan, hint, skip |
| limit, blocked, stale | A run limit, a builder's blocker, a lost session | retry, hint, skip, plan |
| scope | A builder asks for files it does not own | plan, dismiss |
| question | The assigner asked you something | answer (plan) |
| final | The once-per-job review asks for changes or could not run | accept, retry, plan |
| accept | The finished job could not be accepted (combined checks, all tasks skipped) | retry, plan |

\* hint is not offered when only the reviewer failed; a merge retry is offered
only when the combined checks can rerun unchanged.

Automatic retries come first, so you only see what they could not fix. Review
findings go back to the same builder up to 3 times. A failed gate or a crash is
retried once by a builder (in separate tasks, a tester's failure comes to you at
once, because a tester cannot tell whether its tests or the code are wrong). A
provider's usage limit is never a problem: the task resumes by itself when the
provider is back.

The same tool or setup problem in two tasks of one job is the environment, not
the tasks. That job then starts nothing new until you retry one of them, so no
tokens are spent on sessions that would fail the same way.

## Replies, cheapest first

| Reply | What it does | Tokens |
|---|---|---|
| retry | Runs the stopped step again as it is, after you fixed the cause | Only the rerun |
| accept | Takes the builder's reviewed commit; its tests must still pass | None |
| skip | Cancels the task and every task that depends on it | None |
| dismiss | Closes a request for more files; the task keeps its scope | None |
| hint &lt;words&gt; | The same builder continues its own session with your words | One builder turn |
| plan &lt;words&gt; | The assigner re-plans the job with your words | One planning session |

Only `plan` starts a model that was not already working on the task. If the new
plan leaves the task stopped, the problem comes back to you.

Answer in the **Jobs** tab, from the terminal (`agentkit problem list`,
`agentkit problem act CODE retry`), or from your phone.

## Your phone (email)

Turn it on in **Settings > Phone (email)**. Use a separate mailbox for AgentKit
(for Gmail, create an app password). Enter your own address, the one your phone
reads. The settings serve every repository. The password is encrypted for your
Windows user (DPAPI) and is never shown, logged or put into a prompt.

- Each new problem sends one email:
  - the subject carries its code, for example `[K7Q2XM]`;
  - the body says what happened, whether the rest of the job is still working, and the replies.
- Reply with one word, and your own words after `hint`, `plan` or `answer`. You can
  also reply with:
  - `status`: what the job is doing;
  - `pause`: nothing new starts in this job, and running sessions finish;
  - `resume`: the job goes on.
- AgentKit confirms every action by email. A reply it cannot act on gets the reason
  and the valid replies back.
- It acts only on mail that:
  - comes from your address;
  - the receiving server confirmed (DKIM or SPF);
  - carries an open problem's code.

  Anything else is left unread.
- Replies are read about once a minute while the repository is started. A mail
  failure never stops the supervisor; mail is retried after five minutes.
- A finished job is announced too. You can turn that off.

### SMS

SMS needs a paid provider with a phone number that can receive replies, for
example Twilio. Nothing else in the design changes: same codes, same one-word
replies. It is not built yet. Most phones show new email as a notification, so
email covers the same need without an account or fees.
