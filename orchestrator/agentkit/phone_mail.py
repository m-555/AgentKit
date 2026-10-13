"""Email from and to the user's phone: settings, the protected password, sending and reading.

One mailbox serves every repository: AgentKit sends from it and reads the user's
replies in it. Its address and servers live in the AgentKit home folder; its app
password is encrypted for this Windows user (DPAPI) and never shown again, logged
or put in a prompt. Only standard-library SMTP and IMAP over TLS are used.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import default as POLICY
from email.utils import make_msgid, parseaddr

from .locking import atomic_write
from .project_registry import home

DEFAULTS = {"enabled": False, "agent_address": "", "user_address": "", "smtp_host": "smtp.gmail.com",
            "smtp_port": 465, "imap_host": "imap.gmail.com", "imap_port": 993, "notify_done": True}
TIMEOUT = 20


def settings() -> dict:
    try:
        saved = json.loads((home() / "phone.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save(changes: dict, password: str | None = None) -> dict:
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("phone settings support " + ", ".join(DEFAULTS))
    merged = {**settings(), **changes}
    for key in ("agent_address", "user_address"):
        value = str(merged[key]).strip()
        if value and "@" not in parseaddr(value)[1]:
            raise ValueError(f"{key.replace('_', ' ')} must be an email address")
        merged[key] = value
    for key in ("smtp_port", "imap_port"):
        if type(merged[key]) is not int or not 0 < merged[key] < 65536:
            raise ValueError(f"{key.replace('_', ' ')} must be a port number")
    for key in ("enabled", "notify_done"):
        if type(merged[key]) is not bool:
            raise ValueError(f"{key.replace('_', ' ')} is on or off")
    if merged["enabled"] and not (merged["agent_address"] and merged["user_address"]):
        raise ValueError("turning on needs the AgentKit mailbox and your address")
    home().mkdir(parents=True, exist_ok=True)
    if password:
        atomic_write(home() / "phone.secret", _protect(password.encode("utf-8")).hex())
    atomic_write(home() / "phone.json", json.dumps(merged, indent=2) + "\n")
    return merged


def password() -> str | None:
    """The mailbox app password, or None when none is saved."""
    if os.environ.get("AGENTKIT_PHONE_PASSWORD"):
        return os.environ["AGENTKIT_PHONE_PASSWORD"]
    try:
        stored = (home() / "phone.secret").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _unprotect(bytes.fromhex(stored)).decode("utf-8") if stored else None


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt = ctypes.windll.crypt32
    call = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # 0x1: CRYPTPROTECT_UI_FORBIDDEN; bound to this Windows user.
    if not call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise OSError("Windows could not " + ("protect" if protect else "read") + " the saved mailbox password")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("saving the password needs Windows; elsewhere set AGENTKIT_PHONE_PASSWORD")
    return _dpapi(data, True)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("the saved password can only be read on Windows; set AGENTKIT_PHONE_PASSWORD")
    return _dpapi(data, False)


def compose(config: dict, subject: str, body: str, *, reply_to: str | None = None) -> EmailMessage:
    message = EmailMessage(policy=POLICY)
    message["From"] = config["agent_address"]
    message["To"] = config["user_address"]
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="agentkit.local")
    if reply_to:
        message["In-Reply-To"] = message["References"] = reply_to
    message.set_content(body)
    return message


def send(config: dict, secret: str, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=TIMEOUT, context=context) as smtp:
        smtp.login(config["agent_address"], secret)
        smtp.send_message(message)


def replies(config: dict, secret: str, handle) -> int:
    """Read unread mail from the user; `handle(message)` returns True for a reply it answered.

    Only answered replies are marked read, so another repository's service can answer
    the rest. Returns how many were answered.
    """
    answered = 0
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"], ssl_context=context, timeout=TIMEOUT) as imap:
        imap.login(config["agent_address"], secret)
        imap.select("INBOX")
        _, found = imap.uid("SEARCH", "UNSEEN", "FROM", f'"{config["user_address"]}"')
        for uid in (found[0] or b"").decode().split():
            _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
            if raw is None:
                continue
            if handle(email.message_from_bytes(raw, policy=POLICY)):
                imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                answered += 1
    return answered
