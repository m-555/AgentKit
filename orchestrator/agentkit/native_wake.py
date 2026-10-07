"""Opt-in, one-shot same-chat wake diagnostic. Private protocol, no production guarantee."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .locking import atomic_write, exclusive
from .native_ipc import Connection, overview
from .secrets import redact

TERMINAL = {"completed", "failed", "interrupted"}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A timezone-aware ISO timestamp is required")
    return parsed


def quota_summary(value) -> dict:
    """Keep only bounded official quota metadata, never authentication or raw events."""
    windows = []
    for row in (value.get("windows") or [])[:32]:
        percent = row.get("used_percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not math.isfinite(percent):
            continue
        windows.append({"used_percent": percent, "resets_at": row.get("resets_at"),
                        "window": str(row.get("window", ""))[:80]})
    complete = value.get("complete") is True and bool(windows)
    confirmed = complete and value.get("available") is True and all(0 <= w["used_percent"] < 100 for w in windows)
    return {"provider_confirmed_available": confirmed, "complete": complete,
            "exhausted": any(w["used_percent"] >= 100 for w in windows), "windows": windows}


def delivery_payload(thread: str, prompt: str, message_id: str) -> dict:
    # The native input uses snake_case text_elements, including when it is empty.
    return {"conversationId": thread, "turnStart": {
        "request": {"threadId": thread, "clientUserMessageId": message_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}]},
        "context": {"inheritThreadSettings": True}}}


def resume_prompt(result_path: Path, recovered: bool) -> str:
    evidence = ("Actual 100% exhaustion was observed before provider-confirmed availability."
                if recovered else "No 100% exhaustion was observed; this tests idle wake, not a proven quota reset.")
    runtime = next((parent for parent in result_path.parents
                    if parent.name == "runtime" and parent.parent.name == ".ai"), None)
    if runtime is None:
        raise ValueError("Wake result must be inside the project's .ai/runtime directory")
    checkpoint = runtime / "manager-checkpoint.json"
    legacy = runtime / "setup" / "quota-reset-manager-checkpoint.json"
    if not checkpoint.exists() and legacy.exists():
        checkpoint = legacy
    return (
        "Authorized one-shot same-chat wake diagnostic. " + evidence + " "
        f"Read the watcher result at {result_path}, the saved manager checkpoint at "
        f"{checkpoint} if it exists, "
        "and current authoritative job/runtime state before continuing. "
        "Resume only the latest user-authorized work for this project. Read current user instructions "
        "and project policy for the manager, reviewer, worker roles, models and session limits; "
        "saved checkpoints can be outdated. Preserve existing features. If execution is paused or "
        "readiness checks are incomplete, do not activate jobs or launch workers. Begin implementation "
        "only if current checkpoints, required checks and user authorization prove readiness. "
        "Do not claim wake passed until reading the result; submission acceptance alone does not "
        "prove turn completion."
    )


def _create(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(redact(value), indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _availability():
    from .adapters.codex import CodexAdapter
    # This adapter uses official account/rateLimits/read only, never thread/resume.
    return CodexAdapter().check_availability()


def run(root: Path, thread: str, not_before: datetime, deadline: datetime, poll_seconds: float = 300,
        *, quota_recovery_only: bool = False, renew: bool = False, scope: str = "", connect=Connection, availability=_availability, now=lambda: datetime.now(UTC), pause=time.sleep) -> dict:
    """Run only when explicitly invoked; injected dependencies make tests provider-free."""
    started = now()
    if not thread or not root.is_dir():
        raise ValueError("An existing project root and explicit native thread are required")
    if any(value.tzinfo is None or value.utcoffset() is None for value in (started, not_before, deadline)):
        raise ValueError("Wake times must include a timezone")
    if deadline <= not_before or deadline <= started or deadline > started + timedelta(days=1):
        raise ValueError("Explicit deadline must follow not-before and be within 24 hours")
    if not 1 <= poll_seconds <= 3600:
        raise ValueError("poll-seconds must be between 1 and 3600")
    if os.environ.get("AGENTKIT_PROCESS") or os.environ.get("AGENTKIT_TASK"):
        raise PermissionError("A supervised session cannot arm a native manager wake")
    native_thread = os.environ.get("CODEX_THREAD_ID")
    if native_thread and native_thread != thread:
        raise PermissionError("Native wake must target the authorizing chat")
    key = hashlib.sha256(thread.encode()).hexdigest()[:24]
    directory = root / ".ai" / "runtime" / ("native-quota-wake" if quota_recovery_only else "native-wake")
    if scope:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", scope):
            raise ValueError("Wake scope must be a bounded filename-safe authorization label")
        directory = directory / "scopes" / scope
    arm_path, claim_path = directory / f"{key}.armed.json", directory / f"{key}.claimed.json"
    result_path = directory / f"{key}.result.json"
    result: dict = {"thread": thread, "private_protocol": True, "production_guarantee": False,
              "not_before": not_before.isoformat(), "deadline": deadline.isoformat(),
              "turn_start_attempts": 0, "quota_recovery_only": quota_recovery_only, "actual_exhaustion_observed": False,
              "provider_confirmed_recovery": False, "native_turn_completed": False, "ui_health_verified": False, "status": "starting"}
    connection: Any = None
    owner = None
    armed = False

    def save():
        from .native_recovery import save as recovery_save
        recovery_save(root, result)
        atomic_write(result_path, json.dumps(redact(result), indent=2) + "\n")

    def snapshot():
        from .native_wake_control import require_enabled
        require_enabled(root, thread)
        if connection.current_peer() != result["pipe_peer"]:
            raise RuntimeError("VS Code pipe process changed; cancel without reconnect")
        if connection.discover_owner(thread) != owner:
            raise RuntimeError("Native chat owner changed; cancel without delivery")
        summary = overview(connection.snapshot(owner, thread))
        if summary["latest_turn_id"] != result["authorizing_turn"]:
            raise RuntimeError("An intervening turn changed authorization; cancel without delivery")
        return summary

    def idle(summary):
        # VS Code can retain systemError after a quota failure even when the
        # provider has recovered. Only observed quota recovery may attempt this state;
        # the unchanged authorizing turn, pending-input and fresh quota checks
        # below still apply before the one-shot claim is consumed.
        recovered_error = (result["provider_confirmed_recovery"]
                           and summary["runtime"] == "systemError" and summary["latest_status"] == "failed")
        return ((summary["runtime"] == "idle" or recovered_error)
                and summary["latest_status"] in TERMINAL and not summary["pending"])

    try:
        with exclusive(root, f"native-wake-{key}-{quota_recovery_only}", timeout=0):
            if (arm_path.exists() or claim_path.exists()) and not renew:
                return {"status": "already_armed_no_retry", "turn_start_attempts": 0,
                        "result_path": str(result_path)}
            from .native_wake_control import require_enabled
            require_enabled(root, thread)
            connection = connect()
            connection.initialize()
            owner = connection.discover_owner(thread)
            summary = overview(connection.snapshot(owner, thread))
            if (summary["runtime"] != "active" or summary["latest_status"] != "inProgress"
                    or not summary["latest_turn_id"]):
                raise RuntimeError("Arm during the active authorizing native turn")
            if renew and (arm_path.exists() or claim_path.exists()):
                from .native_wake_control import archive_finished
                archive_finished(root, directory, key)
            result.update(authorizing_turn=summary["latest_turn_id"], authorizing_turn_count=summary["turn_count"],
                          owner=owner, pipe_peer=connection.current_peer())
            from . import native_recovery
            native_recovery.arm(root, result)
            _create(arm_path, result)
            armed = True
            baseline = quota_summary(availability())
            result["baseline_quota_observed_at"] = now().isoformat()
            result.update(baseline_quota=baseline, latest_quota=baseline,
                          actual_exhaustion_observed=baseline["exhausted"], status="armed_waiting")
            save()
            while now() < deadline:
                current = snapshot()
                result["last_snapshot"] = current
                result["last_native_state_observed_at"] = now().isoformat()
                quota = quota_summary(availability())
                result["latest_quota"] = quota
                result["last_quota_observed_at"] = now().isoformat()
                result["actual_exhaustion_observed"] |= quota["exhausted"]
                result["provider_confirmed_recovery"] = bool(
                    now() >= not_before and result["actual_exhaustion_observed"] and quota["provider_confirmed_available"])
                if now() < not_before:
                    result["status"] = "waiting_for_earliest_delivery"
                else:
                    result["status"] = "waiting_for_provider_or_idle"
                    if (quota["provider_confirmed_available"] and idle(current)
                            and (not quota_recovery_only or result["provider_confirmed_recovery"])):
                        pause(min(2, max(0, (deadline - now()).total_seconds())))
                        fresh = snapshot()
                        # A quota-only delivery needs availability confirmed again
                        # immediately before its single consumed delivery claim.
                        fresh_available = True
                        if quota_recovery_only or result["provider_confirmed_recovery"]:
                            fresh_quota = quota_summary(availability())
                            result["latest_quota"] = fresh_quota
                            result["last_quota_observed_at"] = now().isoformat()
                            fresh_available = fresh_quota["provider_confirmed_available"]
                            result["provider_confirmed_recovery"] = bool(fresh_available)
                        if now() < deadline and idle(fresh) and fresh_available:
                            require_enabled(root, thread)
                            native_recovery.claim(root, result, now=now())
                            message_id = str(uuid.uuid4())
                            _create(claim_path, {"attempts": 1, "owner": owner, "thread": thread,
                                                 "message_id": message_id, "claimed_at": now().isoformat()})
                            result.update(turn_start_attempts=1, status="single_delivery_claimed",
                                          wake_kind="quota-recovery" if result["provider_confirmed_recovery"] else "idle-wake-only")
                            save()  # A failed save leaves the claim consumed and sends nothing.
                            reply = connection.request("thread-follower-start-turn", 2,
                                delivery_payload(thread, resume_prompt(result_path, result["provider_confirmed_recovery"]), message_id), owner)
                            if reply.get("resultType") != "success":
                                result["status"] = "native_delivery_rejected_no_retry"
                                break
                            turn = (reply.get("result") or {}).get("result", {}).get("turn", {})
                            if (reply.get("method") != "thread-follower-start-turn"
                                    or reply.get("handledByClientId") != owner or not turn.get("id")):
                                raise RuntimeError("Native delivery reply unknown; claim consumed, no retry")
                            result.update(status="native_turn_accepted_completion_unverified", submitted_turn_id=turn["id"])
                            save()
                            until = deadline
                            while now() < until:
                                if connection.current_peer() != result["pipe_peer"] or connection.discover_owner(thread) != owner:
                                    raise RuntimeError("Native owner changed after delivery")
                                after = overview(connection.snapshot(owner, thread))
                                result["after_snapshot"] = after
                                if after["latest_turn_id"] != turn["id"]:
                                    break
                                if after["latest_status"] == "inProgress" and not result.get("native_turn_started"):
                                    result.update(native_turn_started=True, status="native_turn_started_completion_unverified")
                                    save()
                                if after["latest_status"] in TERMINAL:
                                    result["native_turn_completed"] = after["latest_status"] == "completed"
                                    result["status"] = "same_native_chat_turn_completed" if result["native_turn_completed"] else "delivered_turn_failed_or_interrupted"
                                    break
                                pause(min(3, max(0, (until - now()).total_seconds())))
                            break
                save()
                delay = min(poll_seconds, (deadline - now()).total_seconds())
                if now() < not_before:
                    delay = min(delay, (not_before - now()).total_seconds())
                pause(max(0, delay))
            else:
                result["status"] = "deadline_reached_no_delivery"
    except Exception as error:
        result["status"] = "delivery_outcome_unknown_no_retry" if result["turn_start_attempts"] else "cancelled_no_delivery"
        result["error"] = str(redact(str(error)))[:1000]
    finally:
        try:
            if armed:
                save()
        finally:
            if connection is not None:
                if owner:
                    with suppress(Exception):
                        connection.following(owner, thread, False)
                connection.close()
    result["result_path"] = str(result_path)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--require-quota-recovery", action="store_true",
                        help="Never deliver for idle alone: observe exhaustion then confirmed availability")
    parser.add_argument("--scope", default="", help="New explicitly authorized test scope; does not replay old unresolved delivery")
    parser.add_argument("--renew", action="store_true", help="Explicitly renew after a proven terminal one-shot; unresolved delivery cannot be retried")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--not-before", type=timestamp, required=True)
    parser.add_argument("--deadline", type=timestamp, required=True)
    parser.add_argument("--poll-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    result = run(args.root.resolve(), args.thread, args.not_before, args.deadline, args.poll_seconds, quota_recovery_only=args.require_quota_recovery, renew=args.renew, scope=args.scope)
    print(json.dumps(redact(result)), flush=True)
    return 0 if result.get("submitted_turn_id") else 2


if __name__ == "__main__":
    raise SystemExit(main())
