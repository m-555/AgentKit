"""Code accepts a finished job; with `review: once`, after one AI review of the whole change.

The manager used to wake when every task was done, re-read the job and accept it.
That check repeated what the reviews had already seen. Now the supervisor does it:
every required task approved and merged, the combined checks passing, and, when the
project reviews once per job, one independent review of the combined result. A
finished job never wakes the assigner. Whatever stops acceptance becomes a problem
for the user (problems.py): the combined checks failing, a final review asking for
changes or unable to run, or a job whose every task was skipped.
"""
from __future__ import annotations

from . import db, integrator, repo, workflow
from .secrets import redact_text

#: Job stops this module makes; problems.py turns each into a question for the user.
ACCEPT = "acceptance: "
FINAL = "final review: "
VERDICTS = ("PASS", "CHANGES", "REJECT", "BLOCKED")
#: Characters of combined diff in the final reviewer's prompt; it reads the rest itself.
LIMIT = 40000


def latest(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM job_reviews WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return dict(row) if row else None


def record(conn, project, job_id: str, head: str, verdict: str, reviewer: str, evidence: str) -> None:
    """A review of the whole job at the exact combined commit; the next supervisor pass acts on it."""
    verdict = verdict.upper()
    if verdict not in (*VERDICTS, "RETRY") or not evidence.strip():
        raise ValueError("a verdict (PASS, CHANGES, REJECT or BLOCKED) and concrete evidence are required")
    if repo.head_commit(integrator.integration_worktree(project)) != head:
        raise ValueError("the combined result changed; review its current commit")
    conn.execute("INSERT INTO job_reviews(job_id,head_sha,verdict,reviewer,evidence,created_at) VALUES(?,?,?,?,?,?)",
                 (job_id, head, verdict, reviewer, str(db.redact(evidence))[:8000], db.utcnow()))
    db.log_event(conn, None, "job_review", cause=evidence[:1000],
                 detail={"job": job_id, "head": head, "verdict": verdict, "reviewer": reviewer})


def user_verdict(conn, project, job_id: str, verdict: str, by: str) -> None:
    """The user overrides the final review (PASS) or asks for a new one (RETRY) at the current commit."""
    head = repo.head_commit(integrator.integration_worktree(project))
    words = "The user accepted the combined result." if verdict == "PASS" else "The user asked for a new final review."
    record(conn, project, job_id, head, verdict, f"user:{by}", words)


def _hold(conn, job_id: str, error: str) -> None:
    conn.execute("UPDATE jobs SET status='BLOCKED',last_error=?,updated_at=? WHERE id=? AND status='ACTIVE'",
                 (error[:1500], db.utcnow(), job_id))
    db.log_event(conn, None, "job_acceptance_stopped", cause=error[:1000], detail={"job": job_id})


def step(conn, project, job: dict, runtime: dict, tasks: list[dict], launch) -> str | None:
    """Accept a finished job, or take its next step toward acceptance. `launch` starts the final reviewer."""
    from .review_policy import mode
    review = mode(project)
    if (review == "human" or not workflow.enabled(project) or not tasks or runtime["status"] != "ACTIVE"
            or runtime["planned_revision"] != job["revision"] or any(t["status"] not in ("DONE", "CANCELLED") for t in tasks)):
        return None
    if not any(t["status"] == "DONE" for t in tasks):
        _hold(conn, job["id"], ACCEPT + "every task was skipped or cancelled, so nothing was built. Re-plan it, "
              "or leave it.")
        return None
    head = repo.head_commit(integrator.integration_worktree(project))
    if review == "once":
        last = latest(conn, job["id"])
        if not last or last["head_sha"] != head or last["verdict"] == "RETRY":
            from . import processes
            if any(p["purpose"] == "final_review" and p["job_id"] == job["id"] for p in processes.owning(conn)):
                return None
            since = last["created_at"] if last and last["head_sha"] == head else ""
            ended = conn.execute("SELECT COUNT(*) FROM processes WHERE purpose='final_review' AND job_id=? AND "
                                 "expected_head=? AND status NOT IN ('STARTING','RUNNING') AND started_at>?",
                                 (job["id"], head, since)).fetchone()[0]
            if ended >= 2:
                _hold(conn, job["id"], FINAL + "could not review: the final reviewer stopped twice without a "
                      "verdict (its output is in the Agents tab)")
            else:
                launch()
            return None
        if last["verdict"] != "PASS":
            what = "could not review" if last["verdict"] == "BLOCKED" else last["verdict"]
            _hold(conn, job["id"], f"{FINAL}{what}: {last['evidence']}")
            return None
    from .supervisor import accept_job
    try:
        done = accept_job(project.root, job["id"], job["revision"],
                          "Accepted by code: every required task approved and merged; combined checks passed"
                          + ("; the final review passed" if review == "once" else "") + ".")
    except (ValueError, PermissionError) as exc:
        _hold(conn, job["id"], ACCEPT + str(exc))
        return None
    return f"job {job['id']} accepted at {done[:12]}"


def body(conn, project, job: dict, tasks: list[dict], head: str, *, limit: int | None = None) -> str:
    """The final reviewer's instruction and packet: intent, tasks and the combined change."""
    size = LIMIT if limit is None else limit
    work = integrator.integration_worktree(project)
    done = sorted((t for t in tasks if t["status"] == "DONE"), key=lambda t: t["id"])
    base = next((t["base_sha"] for t in done if t.get("base_sha") and repo.is_ancestor(work, t["base_sha"], head)), None)
    lines = [f"Review the whole of job {job['id']} once, at the combined commit {head}. Every task passed its own "
             "tests and merged; no one has reviewed the code. Check the combined change against every user request "
             "and acceptance criterion below, and look for defects the tests would not catch.",
             f"Call job_review_submit(job_id='{job['id']}', head_sha='{head}', verdict='PASS' or 'CHANGES' or "
             "'REJECT', evidence=concrete findings with file and line). Do not fix source. BLOCKED means a tool, "
             "permission or setup problem kept you from reviewing; the user is asked and nobody redoes work.",
             "## Job intent", workflow.intent_text(job), "## Tasks"]
    lines += [f"- task {t['id']}: {t['title']}" for t in done]
    if base:
        files = repo.diff_files(work, base)
        diff = redact_text(repo._git(["diff", "--no-color", f"{base}..{head}"], work))
        lines += [f"## Combined change since {base[:12]}", f"Changed files ({len(files)}): " + ", ".join(files),
                  "Changes other jobs merged in the meantime can appear here too."]
        if size:
            lines += ["```diff", diff[:size], "```"]
        if len(diff) > size:
            lines.append(f"[The diff is {len(diff):,} characters; the first {size:,} are above. Read the rest of the "
                         "changed files with source_read(where='integration', paths=[...]).]")
    else:
        lines.append("No recorded base for this job; read the changed files with source_read(where='integration').")
    lines.append("Read anything else with source_list, source_read or source_search (where='integration').")
    return "\n".join(lines)
