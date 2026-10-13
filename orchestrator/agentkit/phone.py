"""Problems reach the user's phone by email, and the user's reply answers them.

When a task stops, one short email says what happened, whether the rest of the job
is still working, and the replies that fix it, cheapest first. Nothing spends
tokens until the user answers. A reply is one word, with the user's own words after
hint, plan or answer:

    retry | accept | skip | dismiss | hint <words> | plan <words> | answer <words>
    status | pause | resume

AgentKit acts only on a reply from the user's own address that carries the open
problem's code ([K7Q2XM] in the subject). It confirms what it did by email, and
answers a reply it cannot act on with the reason and the valid replies. A finished
job is announced too, unless the user turned that off.
"""
from __future__ import annotations

import html
import imaplib
import json
import re
import smtplib
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from . import db, phone_mail, problems
from .locking import atomic_write

POLL_SECONDS = 60
RETRY_SECONDS = 300
HELP = {
    "retry": "run it again as it is, after you fixed the tool, login or setup",
    "accept": "take the builder's commit as it is (its tests still have to pass)",
    "skip": "cancel this task and every task that depends on it",
    "dismiss": "close the request; the task keeps its files",
    "hint": "hint <your words>: the same builder continues with your words",
    "plan": "plan <your words>: the assigner re-plans the job with your words",
}
CODE = re.compile(r"\[([A-Z0-9]{6})\]")
# Where the quoted original starts in a reply; mail apps may wrap the "On ... wrote:" line.
QUOTE = re.compile(r"^(>|On .*(wrote|<)|.+ wrote:$|-+ ?Original Message|_{5,}|From: |Sent from my )", re.I)
WORKING = ("READY", "LEASED", "RUNNING", "VERIFYING", "REVIEW", "INTEGRATION_READY", "INTEGRATING")


def subject(project, problem: dict) -> str:
    return f"[AgentKit {project.name}] {problem['title']} [{problem['code']}]"[:240]


def _replies(problem: dict) -> list[str]:
    if problem["kind"] == "question":
        return ["  answer <your words> - the assigner plans with your answer"]
    if problem["kind"] == "final":
        return ["  accept - take the combined result as it is",
                "  retry - review the whole job again",
                "  plan - the assigner fixes what the review found (add your words if you like)"]
    return [f"  {option} - {HELP[option]}" for option in problem["options"]]


def body(conn, project, problem: dict) -> str:
    marks = ",".join("?" * len(WORKING))
    working = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE job_id=? AND status IN ({marks})",
                           (problem["job_id"], *WORKING)).fetchone()[0]
    rest = (f"The rest of job {problem['job_id']} keeps going: {working} task(s) still working." if working else
            f"Nothing else in job {problem['job_id']} is working now.")
    lines = [problem["detail"], "", rest, ""]
    if problem["options"]:
        lines += ["Reply with one word, cheapest first:", *_replies(problem)]
    else:
        lines.append("This only informs you; change it in the view.")
    lines += ["  status - what the job is doing", "  pause / resume - nothing new starts in this job, or it goes on",
              "", "Nothing spends tokens until you reply.", f"Code {problem['code']} · job {problem['job_id']} · {project.root}"]
    return "\n".join(lines)


def plain(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = html.unescape(re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I))
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def read_command(message, config: dict) -> tuple[str, str, str] | None:
    """(code, reply, words) from the user's own mail, or None for anything else."""
    sender = parseaddr(str(message.get("From") or ""))[1].casefold()
    if not sender or sender != parseaddr(config["user_address"])[1].casefold():
        return None
    checks = str(message.get("Authentication-Results") or "").lower()
    if checks and "dkim=pass" not in checks and "spf=pass" not in checks:
        return None  # the receiving server could not confirm the sender
    text = plain(message)
    found = CODE.search(str(message.get("Subject") or "")) or CODE.search(text)
    if not found:
        return None
    lines = []
    for line in text.splitlines():
        if QUOTE.match(line.strip()):
            break
        if line.strip():
            lines.append(line.strip())
    first, _, rest = " ".join(lines).partition(" ")
    return found.group(1), first.lower().strip(".,!:;"), rest.strip()


def pause(conn, job_id: str, on: bool) -> str:
    if on:
        changed = conn.execute("UPDATE jobs SET status='BLOCKED',last_error='paused: by you, from your phone',updated_at=? "
                               "WHERE id=? AND status IN ('ACTIVE','PLANNING')", (db.utcnow(), job_id)).rowcount
        return (f"Job {job_id} paused: nothing new starts; running sessions finish." if changed else
                f"Job {job_id} was not running, so nothing changed.")
    changed = conn.execute("UPDATE jobs SET status=CASE WHEN planned_revision=revision THEN 'ACTIVE' ELSE 'PLANNING' END,"
                           "last_error=NULL,updated_at=? WHERE id=? AND status='BLOCKED' AND last_error LIKE 'paused: %'",
                           (db.utcnow(), job_id)).rowcount
    return f"Job {job_id} goes on." if changed else f"Job {job_id} was not paused, so nothing changed."


def status(conn, job_id: str) -> str:
    row = conn.execute("SELECT status,last_error FROM jobs WHERE id=?", (job_id,)).fetchone()
    counts = conn.execute("SELECT status,COUNT(*) FROM tasks WHERE job_id=? GROUP BY status", (job_id,)).fetchall()
    waiting = [p for p in problems.listing(conn, job_id) if p["status"] == "OPEN"]
    lines = [f"Job {job_id}: {row['status'] if row else 'unknown'}"
             + (f" ({row['last_error']})" if row and row["last_error"] else ""),
             "Tasks: " + (", ".join(f"{n} {s.lower()}" for s, n in counts) or "none yet")]
    lines += [f"Waiting for you: [{p['code']}] {p['title']}" for p in waiting]
    return "\n".join(lines)


def handle(conn, project, config: dict, message, deliver) -> bool:
    """Answer one reply when it belongs to an open problem of this repository."""
    parsed = read_command(message, config)
    if parsed is None:
        return False
    code, action, words = parsed
    row = problems.find(conn, code)
    if row is None:
        return False  # another repository's problem: leave it unread for that service
    try:
        if action == "status":
            answer = status(conn, row["job_id"])
        elif action in ("pause", "resume"):
            answer = pause(conn, row["job_id"], action == "pause")
        else:
            answer = "Done: " + problems.act_locked(conn, project, code, action, words, by="phone")
    except (ValueError, PermissionError) as exc:
        answer = f"Not done: {exc}"
        if row["status"] == "OPEN" and row["options"]:
            answer += "\n\nReplies for this problem:\n" + "\n".join(_replies(row))
    deliver(phone_mail.compose(config, "Re: " + subject(project, row), answer,
                               reply_to=str(message.get("Message-ID") or "") or None))
    return True


def _state_path(project):
    return project.root / ".ai" / "runtime" / "phone.json"


def state(project) -> dict:
    try:
        return json.loads(_state_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tick(conn, project, *, transport=phone_mail, now: datetime | None = None) -> list[str]:
    """Send what is new and read the user's replies; never raises for a mail problem."""
    config = transport.settings()
    if not config["enabled"]:
        return []
    now = now or datetime.now(UTC)
    saved = state(project)
    retry_at = db.parse_ts(saved.get("retry_at"))
    if retry_at and now < retry_at:
        return []
    notes = []
    try:
        secret = transport.password()
        if not secret:
            raise OSError("no mailbox password is saved; save it in the view's Settings tab")
        for row in conn.execute("SELECT * FROM problems WHERE status='OPEN' AND notified_at IS NULL ORDER BY id").fetchall():
            problem = problems.find(conn, row["id"])
            if problem is None:
                continue
            transport.send(config, secret, phone_mail.compose(config, subject(project, problem), body(conn, project, problem)))
            conn.execute("UPDATE problems SET notified_at=? WHERE id=?", (db.utcnow(), row["id"]))
            notes.append(f"sent problem {problem['code']} to your phone")
        last_event = int(saved.get("last_event") or 0)
        if not last_event:  # first run: jobs finished before the phone was set up are not news
            last_event = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
        for event in conn.execute("SELECT id,detail FROM events WHERE kind='job_accepted' AND id>? ORDER BY id",
                                  (last_event,)).fetchall():
            detail = json.loads(event["detail"] or "{}")
            if config["notify_done"]:
                transport.send(config, secret, phone_mail.compose(
                    config, f"[AgentKit {project.name}] Job {detail.get('job')} is done",
                    f"Job {detail.get('job')} was accepted at {str(detail.get('head'))[:12]}.\n{project.root}"))
            last_event = event["id"]
        saved["last_event"] = last_event
        polled = db.parse_ts(saved.get("polled_at"))
        if not polled or now - polled >= timedelta(seconds=POLL_SECONDS):
            answered = transport.replies(config, secret, lambda message: handle(
                conn, project, config, message, lambda reply: transport.send(config, secret, reply)))
            saved["polled_at"] = now.isoformat()
            if answered:
                notes.append(f"answered {answered} reply(ies) from your phone")
        saved.pop("error", None)
        saved.pop("retry_at", None)
    except (OSError, smtplib.SMTPException, imaplib.IMAP4.error) as exc:
        saved["error"] = str(exc)[:300]
        saved["retry_at"] = (now + timedelta(seconds=RETRY_SECONDS)).isoformat()
        db.log_event(conn, None, "phone_error", cause=saved["error"], effect="mail retried in 5 minutes")
    _state_path(project).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_state_path(project), json.dumps(saved, indent=2) + "\n")
    return notes
