"""Jobs written in the view: the user's own words, with screenshots.

The words are kept exactly as written (jobs.py). Screenshots are stored with the
runtime state, never committed, and each request names its own, so the assigner
and the builders open them with the job_attachment tool. Only real PNG, JPEG, GIF
or WebP images are kept; nothing else the browser sends is written.
"""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from . import jobs

MAX_IMAGES = 10
MAX_BYTES = 8 * 1024 * 1024
NAME = re.compile(r"r\d{1,4}-\d{1,2}\.(?:png|jpg|gif|webp)")


def folder(root, job_id: str) -> Path:
    jobs.path(root, job_id)  # the same slug rule as the job file
    return Path(root) / ".ai" / "runtime" / "attachments" / job_id


def _kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _decode(images) -> list[tuple[bytes, str]]:
    if images is None:
        return []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError(f"attach at most {MAX_IMAGES} screenshots")
    out = []
    for index, image in enumerate(images, 1):
        text = image.get("data") if isinstance(image, dict) else None
        if not isinstance(text, str):
            raise ValueError(f"screenshot {index} has no image data")
        try:
            data = base64.b64decode(text.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"screenshot {index} is not valid image data") from None
        if len(data) > MAX_BYTES:
            raise ValueError(f"screenshot {index} is over {MAX_BYTES // (1024 * 1024)} MB")
        kind = _kind(data)
        if kind is None:
            raise ValueError(f"screenshot {index} is not a PNG, JPEG, GIF or WebP image")
        out.append((data, kind))
    return out


def _save(root, job_id: str, revision: int, decoded) -> list[str]:
    target = folder(root, job_id)
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for index, (data, kind) in enumerate(decoded, 1):
        name = f"r{revision}-{index}.{kind}"
        (target / name).write_bytes(data)
        names.append(name)
    return names


def slug(root, text: str) -> str:
    """A job id from the first words of the request, unique in this repository."""
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    base = "-".join(words)[:40].strip("-") or "job"
    candidate, number = base, 2
    while jobs.path(root, candidate).exists():
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def create(root, text: str, *, acceptance: list[str] | None = None, images=None) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("write the job in your own words first")
    lines = [line.strip() for line in (acceptance or []) if isinstance(line, str) and line.strip()]
    decoded = _decode(images)
    job_id = slug(root, text)
    names = _save(root, job_id, 1, decoded)
    try:
        return jobs.create(root, job_id, text.strip(), "auto", acceptance=lines, attachments=names)
    except Exception:
        for name in names:
            (folder(root, job_id) / name).unlink(missing_ok=True)
        raise


def update(root, job_id: str, text: str, *, images=None) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("write the update in your own words first")
    decoded = _decode(images)
    revision = jobs.load(root, job_id)["revision"] + 1
    names = _save(root, job_id, revision, decoded)
    try:
        return jobs.amend(root, job_id, text.strip(), user=True, attachments=names)
    except Exception:
        for name in names:
            (folder(root, job_id) / name).unlink(missing_ok=True)
        raise


def names(root, job_id: str | None) -> list[str]:
    """Every screenshot the user attached to this job, oldest first."""
    if not job_id:
        return []
    try:
        job = jobs.load(root, job_id)
    except (OSError, ValueError):
        return []
    return [name for request in job.get("requests") or [] for name in request.get("attachments") or []]


def read(root, job_id: str, name: str) -> tuple[bytes, str]:
    if not NAME.fullmatch(name) or name not in names(root, job_id):
        raise ValueError(f"job {job_id} has no screenshot named {name!r}")
    path = folder(root, job_id) / name
    if not path.is_file():
        raise ValueError(f"screenshot {name} of job {job_id} is missing from this computer")
    return path.read_bytes(), name.rsplit(".", 1)[1].replace("jpg", "jpeg")
