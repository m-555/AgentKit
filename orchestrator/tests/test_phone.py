"""A stopped task reaches the user's phone by email, and their one-word reply fixes it.

No mail leaves the computer: a fake transport stands in for SMTP and IMAP.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from agentkit import db, phone, phone_mail, problems, reviews
from agentkit import statemachine as sm
from tests.test_infra_stop import DENIED, job_task

USER = "me@example.com"
BOT = "agentkit.bot@example.com"


class Mailbox:
    """Fake SMTP and IMAP: `sent` holds what AgentKit sent, `inbox` what the user wrote."""

    def __init__(self, *, enabled=True, notify_done=True, secret="app-password"):
        self.config = {**phone_mail.DEFAULTS, "enabled": enabled, "agent_address": BOT, "user_address": USER,
                       "notify_done": notify_done}
        self.secret = secret
        self.sent: list[EmailMessage] = []
        self.inbox: list[EmailMessage] = []
        self.fail: Exception | None = None

    def settings(self):
        return self.config

    def password(self):
        return self.secret

    def send(self, config, secret, message):
        if self.fail:
            raise self.fail
        assert secret == self.secret
        self.sent.append(message)

    def replies(self, config, secret, handle):
        answered = [message for message in self.inbox if handle(message)]
        self.inbox = [message for message in self.inbox if message not in answered]
        return len(answered)

    def reply(self, text, *, subject=None, sender=USER, checks="mx.example.com; dkim=pass header.i=@example.com"):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = BOT
        message["Subject"] = subject or "Re: " + str(self.sent[0]["Subject"])
        message["Message-ID"] = "<reply@example.com>"
        if checks:
            message["Authentication-Results"] = checks
        message.set_content(text + "\n\nOn Mon, AgentKit <" + BOT + "> wrote:\n> retry - run it again\n")
        self.inbox.append(message)


def stopped(conn, project_root):
    project, task_id, _, head = job_task(conn, project_root)
    reviews.approve(conn, project, task_id, head, "BLOCKED", "codex:process-4", DENIED)
    problems.sync(conn, project)
    return project, task_id


def later(minutes=2):
    return datetime.now(UTC) + timedelta(minutes=minutes)


def test_a_new_problem_is_mailed_once_with_its_replies(conn, project_root):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    notes = phone.tick(conn, project, transport=box)
    (mail,) = box.sent
    code = problems.listing(conn)[0]["code"]
    assert mail["To"] == USER and mail["From"] == BOT and f"[{code}]" in mail["Subject"]
    text = mail.get_content()
    assert DENIED in text and "retry - " in text and "skip - " in text and "plan <your words>" in text
    assert "hint" not in text.split("Reply with")[1].split("status")[0]  # not a reply for a review that could not run
    assert "Nothing spends tokens until you reply." in text and notes
    phone.tick(conn, project, transport=box, now=later())
    assert len(box.sent) == 1  # never twice


def test_a_one_word_reply_fixes_the_problem_and_is_confirmed(conn, project_root):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("Retry")
    phone.tick(conn, project, transport=box, now=later())
    assert db.get_task(conn, task_id)["status"] == sm.REVIEW
    confirmation = box.sent[-1]
    assert confirmation["Subject"].startswith("Re: ") and confirmation["In-Reply-To"] == "<reply@example.com>"
    assert confirmation.get_content().startswith("Done: ")
    row = conn.execute("SELECT resolution FROM problems").fetchone()
    assert row["resolution"] == "retry by phone"


def test_hint_and_plan_carry_the_users_words(conn, project_root):
    project, task_id, _, _ = job_task(conn, project_root, status="FAILED")
    import yaml
    path = project_root / ".ai/project.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["workflow"] = {"mode": "feature", "review": "ai"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    from agentkit.config import load_project
    project = load_project(project_root)
    db.update_task(conn, task_id, attempts=3, blocker="gate failed: test_limit")
    problems.sync(conn, project)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("hint Use LIMIT from config.py, not a literal")
    phone.tick(conn, project, transport=box, now=later())
    task = db.get_task(conn, task_id)
    assert task["status"] == sm.READY and task["next_action"] == "The user says: Use LIMIT from config.py, not a literal"


def test_a_reply_that_cannot_be_done_says_why_and_lists_the_replies(conn, project_root):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("accept")
    phone.tick(conn, project, transport=box, now=later())
    answer = box.sent[-1].get_content()
    assert answer.startswith("Not done:") and "retry - " in answer
    assert db.get_task(conn, task_id)["status"] == sm.BLOCKED


@pytest.mark.parametrize("sender, checks", [("someone@else.com", "dkim=pass"), (USER, "mx; dkim=fail; spf=fail")])
def test_only_the_users_own_confirmed_mail_is_obeyed(conn, project_root, sender, checks):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("retry", sender=sender, checks=checks)
    phone.tick(conn, project, transport=box, now=later())
    assert db.get_task(conn, task_id)["status"] == sm.BLOCKED
    assert len(box.sent) == 1 and len(box.inbox) == 1  # unanswered, left unread


def test_a_reply_for_another_repository_is_left_unread(conn, project_root):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("retry", subject="Re: [AgentKit other] Task 3 is blocked [QQQQQQ]")
    phone.tick(conn, project, transport=box, now=later())
    assert len(box.inbox) == 1 and db.get_task(conn, task_id)["status"] == sm.BLOCKED


def test_status_pause_and_resume(conn, project_root):
    project, task_id = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    box.reply("status")
    phone.tick(conn, project, transport=box, now=later())
    assert "Job feature: ACTIVE" in box.sent[-1].get_content() and "Waiting for you" in box.sent[-1].get_content()
    box.reply("pause")
    phone.tick(conn, project, transport=box, now=later(4))
    state = conn.execute("SELECT status,last_error FROM jobs").fetchone()
    assert state["status"] == "BLOCKED" and state["last_error"].startswith("paused: ")
    assert problems.sync(conn, project) == []  # a pause is not a question for the user
    box.reply("resume")
    phone.tick(conn, project, transport=box, now=later(6))
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "ACTIVE"


def test_a_finished_job_is_announced(conn, project_root):
    project, _ = stopped(conn, project_root)
    box = Mailbox()
    phone.tick(conn, project, transport=box)
    db.log_event(conn, None, "job_accepted", cause="done", detail={"job": "feature", "head": "a" * 40})
    phone.tick(conn, project, transport=box, now=later())
    assert box.sent[-1]["Subject"].endswith("Job feature is done")
    quiet = Mailbox(notify_done=False)
    db.log_event(conn, None, "job_accepted", cause="done", detail={"job": "feature", "head": "b" * 40})
    phone.tick(conn, project, transport=quiet, now=later(4))
    assert quiet.sent == []


def test_a_mail_failure_waits_and_never_stops_the_supervisor(conn, project_root):
    project, _ = stopped(conn, project_root)
    box = Mailbox()
    box.fail = OSError("connection refused")
    assert phone.tick(conn, project, transport=box) == []
    assert phone.state(project)["error"] == "connection refused"
    box.fail = None
    phone.tick(conn, project, transport=box, now=later(1))
    assert box.sent == []  # waits five minutes before trying again
    phone.tick(conn, project, transport=box, now=later(6))
    assert len(box.sent) == 1 and "error" not in phone.state(project)


def test_nothing_is_sent_while_phone_mail_is_off(conn, project_root):
    project, _ = stopped(conn, project_root)
    box = Mailbox(enabled=False)
    assert phone.tick(conn, project, transport=box) == [] and box.sent == []


def test_settings_are_checked_and_the_password_is_never_stored_in_plain_text(tmp_path):
    with pytest.raises(ValueError, match="email address"):
        phone_mail.save({"user_address": "not an address"})
    with pytest.raises(ValueError, match="needs the AgentKit mailbox"):
        phone_mail.save({"enabled": True})
    saved = phone_mail.save({"agent_address": BOT, "user_address": USER, "enabled": True})
    assert saved["smtp_host"] == "smtp.gmail.com" and phone_mail.settings()["enabled"] is True
    if os.name != "nt":
        pytest.skip("the password is protected with Windows DPAPI")
    phone_mail.save({}, password="abcd efgh ijkl mnop")
    stored = (phone_mail.home() / "phone.secret").read_text(encoding="utf-8")
    assert "abcd" not in stored and b"abcd efgh ijkl mnop".hex() not in stored
    assert phone_mail.password() == "abcd efgh ijkl mnop"


def test_the_reply_is_read_from_html_mail_too():
    message = EmailMessage()
    message["From"] = USER
    message["Subject"] = "Re: [AgentKit demo] Task 4 is blocked [K7Q2XM]"
    message.set_content("<div>skip</div><div><br></div><blockquote>On Mon wrote: retry</blockquote>", subtype="html")
    assert phone.read_command(message, {"user_address": USER})[:2] == ("K7Q2XM", "skip")


def test_the_supervisor_mails_new_problems(conn, project_root, monkeypatch):
    from agentkit import supervisor
    project, _ = stopped(conn, project_root)
    box = Mailbox()
    for name in ("settings", "password", "send", "replies"):
        monkeypatch.setattr(phone_mail, name, getattr(box, name))
    monkeypatch.setattr(supervisor, "refresh_accounts", lambda *args: None)
    supervisor.tick(project_root)
    assert len(box.sent) == 1


def test_the_view_saves_phone_settings_and_sends_a_test(project_root, monkeypatch):
    from tests.test_view_home import call, home_view
    sent = []
    monkeypatch.setattr(phone_mail, "send", lambda config, secret, message: sent.append((secret, message)))
    monkeypatch.setattr(phone_mail, "password", lambda: "app-password")
    with home_view(project_root) as server:
        token = call(server, "/api/reviews")[1]["token"]
        post = {"Content-Type": "application/json", "X-AgentKit-Token": token}
        status, data = call(server, "/api/phone")
        assert status == 200 and data["settings"]["enabled"] is False and "password" not in data["settings"]
        status, result = call(server, "/api/phone", body={"password": "", "settings": {
            "enabled": True, "agent_address": BOT, "user_address": USER}}, headers=post)
        assert status == 200, result
        assert call(server, "/api/phone", body={"password": "", "settings": {"user_address": "nope"}}, headers=post)[0] == 409
        status, result = call(server, "/api/phone-test", body={}, headers=post)
        assert status == 200 and USER in result["message"]
        assert call(server, "/api/phone-test", body={}, headers={"Content-Type": "application/json"})[0] == 403
    assert sent[0][0] == "app-password" and sent[0][1]["To"] == USER


def test_imap_marks_only_answered_replies_read(monkeypatch):
    mine, other = EmailMessage(), EmailMessage()
    mine["Subject"], other["Subject"] = "Re: [A] x [K7Q2XM]", "Re: [B] y [QQQQQQ]"
    store = {b"5": mine.as_bytes(), b"9": other.as_bytes()}
    calls = []

    class FakeImap:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, secret):
            calls.append(("login", user, secret))

        def select(self, box):
            calls.append(("select", box))

        def uid(self, command, *args):
            calls.append((command, *args))
            if command == "SEARCH":
                return "OK", [b"5 9"]
            if command == "FETCH":
                return "OK", [(b"1 (BODY[] {1}", store[args[0].encode()]), b")"]
            return "OK", [b""]

    monkeypatch.setattr(phone_mail.imaplib, "IMAP4_SSL", FakeImap)
    config = {**phone_mail.DEFAULTS, "agent_address": BOT, "user_address": USER}
    answered = phone_mail.replies(config, "pw", lambda message: "K7Q2XM" in str(message["Subject"]))
    assert answered == 1
    assert ("SEARCH", "UNSEEN", "FROM", f'"{USER}"') in calls and ("login", BOT, "pw") in calls
    assert [c for c in calls if c[0] == "STORE"] == [("STORE", "5", "+FLAGS", r"(\Seen)")]


def test_smtp_sends_over_tls_with_the_app_password(monkeypatch):
    seen = {}

    class FakeSmtp:
        def __init__(self, host, port, timeout=None, context=None):
            seen.update(host=host, port=port, tls=context is not None)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, secret):
            seen.update(user=user, secret=secret)

        def send_message(self, message):
            seen["to"] = message["To"]

    monkeypatch.setattr(phone_mail.smtplib, "SMTP_SSL", FakeSmtp)
    config = {**phone_mail.DEFAULTS, "agent_address": BOT, "user_address": USER}
    phone_mail.send(config, "pw", phone_mail.compose(config, "s", "b"))
    assert seen == {"host": "smtp.gmail.com", "port": 465, "tls": True, "user": BOT, "secret": "pw", "to": USER}
