"""`agentkit run --watch` â€” keep going until the queue is idle or needs a human.

The loop is deliberately dull:

    reconcile -> refresh providers -> wake what recovered -> schedule -> sleep

What makes it useful is what it does *not* do. It never sleeps for a provider:
a Claude cooldown parks Claude's tasks and the loop carries straight on to Codex
work. And it holds no state of its own â€” cooldowns and task states live in the
database, so killing the loop mid-cooldown and restarting it later resumes the
wait rather than relaunching workers into a limit that has not lifted.

The sleep interval is derived, not fixed: with a cooldown ending in four hours
and nothing else to do, there is no reason to wake every ten seconds.
"""

from __future__ import annotations

import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, providers, quota, reconcile, scheduler, supervisor
from . import statemachine as sm
from .locking import LockBusy

DEFAULT_POLL_SECONDS = 20
MAX_SLEEP_SECONDS = 300
MIN_SLEEP_SECONDS = 5

#: States that will never progress without a person.
HUMAN_BLOCKED = (sm.NEEDS_REPLAN, sm.FAILED, sm.STALE)


@dataclass
class WatchState:
    iterations: int = 0
    launched: int = 0
    woken: int = 0
    stopped_reason: str = ""
    last_report: str = ""
    history: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{stamp}] {line}"
        self.history.append(entry)
        del self.history[:-200]


class _Stopper:
    """Ctrl-C once asks the loop to finish its pass; twice exits immediately."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: Any = None

    def __enter__(self) -> _Stopper:
        with suppress(ValueError, OSError):    # signal() fails off the main thread
            self._previous = signal.signal(signal.SIGINT, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._previous is not None:
            with suppress(ValueError, OSError):
                signal.signal(signal.SIGINT, self._previous)

    def _handle(self, *_args: object) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print("\n[watch] finishing this pass, then stopping. Ctrl-C again to exit now.")


def idle_reason(conn: Any) -> str | None:
    """Why the loop should stop, or None to keep going."""
    tasks = db.list_tasks(conn)
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status NOT IN ('DONE','BLOCKED')")]
    if jobs:
        return None
    if conn.execute("SELECT 1 FROM jobs WHERE status='BLOCKED'").fetchone() and not any(
        t["status"] in (sm.LEASED, sm.RUNNING, sm.VERIFYING) for t in tasks
    ):
        return "job needs a recorded user decision"
    if any(t["status"] in HUMAN_BLOCKED for t in tasks):
        return "work requires coordinator recovery"
    if not tasks:
        return "no tasks in the graph"

    runnable = [t for t in tasks if str(t["status"]) in (sm.READY, *sm.OCCUPIES_WORKTREE)]
    waiting = [t for t in tasks if quota.is_quota_paused(t)]
    pending = [t for t in tasks if str(t["status"]) in (sm.PLANNED, sm.BLOCKED)]

    if runnable or waiting:
        return None
    if not pending:
        return "queue is empty"
    if all(str(t["status"]) in HUMAN_BLOCKED or not quota.is_quota_paused(t) for t in pending):
        blocked = [t for t in tasks if str(t["status"]) in HUMAN_BLOCKED]
        if blocked and not runnable:
            return f"only human-blocked work remains ({len(blocked)} task(s))"
    return None


def next_sleep(conn: Any, base: int, *, launched: bool) -> int:
    """Poll briskly while work is moving, patiently while waiting on a clock."""
    if launched:
        return MIN_SLEEP_SECONDS
    wait = providers.next_retry(conn)
    if wait is None:
        return base
    # An unrelated cooldown must not slow live workers, review or integration.
    moving = db.list_tasks(conn, (sm.LEASED, sm.RUNNING, sm.VERIFYING, sm.REVIEW,
                                  sm.INTEGRATION_READY, sm.INTEGRATING))
    delay = max(MIN_SLEEP_SECONDS, min(MAX_SLEEP_SECONDS, wait - 2 if wait > 2 else 1))
    return base if moving else delay


def run(
    root: str | Path,
    *,
    max_workers: int | None = None,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    max_iterations: int | None = None,
    dry_run: bool = False,
    sleeper: Any = time.sleep,
    on_pass: Any = None,
) -> WatchState:
    from .locking import exclusive
    with exclusive(root, "watch", timeout=0):
        return _run(root, max_workers=max_workers, poll_seconds=poll_seconds,
                    max_iterations=max_iterations, dry_run=dry_run, sleeper=sleeper, on_pass=on_pass)


def _run(root, *, max_workers=None, poll_seconds=20, max_iterations=None,
         dry_run=False, sleeper=time.sleep, on_pass=None):
    """Supervise until idle. `sleeper` and `max_iterations` exist for tests."""
    root_path = Path(root)
    state = WatchState()

    with _Stopper() as stopper:
        while True:
            if max_iterations is not None and state.iterations >= max_iterations:
                state.stopped_reason = "iteration limit reached"
                break

            state.iterations += 1

            # Restart-safety lives here: reconcile reloads persisted cooldowns and
            # task states, so a fresh process picks up an in-flight wait rather
            # than starting from an optimistic blank slate.
            try:
                notes = supervisor.tick(root_path, dry_run=dry_run)
            except LockBusy as exc:
                state.note(f"{exc}; retrying after {poll_seconds}s")
                sleeper(poll_seconds)
                continue  # Another pass owns authority; do not schedule beside it.
            for note in notes:
                state.note(note)
            reconcile.reconcile(root_path, adopt_running=True)

            conn = db.connect(root_path)
            try:
                cooling = scheduler.unavailable_adapters(conn)
            finally:
                conn.close()

            report = scheduler.run_once(
                root_path, max_workers=max_workers, dry_run=dry_run
            )
            state.launched += len(report.launched)
            state.woken += len(report.woken)
            state.last_report = report.summary()

            for line in report.summary().splitlines():
                if line.strip():
                    state.note(line)
            if on_pass is not None:
                on_pass(state, report)

            conn = db.connect(root_path)
            try:
                reason = idle_reason(conn)
                sleep_for = next_sleep(
                    conn, poll_seconds, launched=bool(report.launched)
                )
                paused = quota.paused_tasks(conn)
            finally:
                conn.close()

            if reason is not None:
                state.stopped_reason = reason
                break
            if stopper.requested:
                state.stopped_reason = "interrupted"
                break

            if cooling:
                state.note(
                    f"{len(paused)} task(s) waiting on "
                    f"{', '.join(sorted(cooling))}; other providers keep running"
                )
            state.note(f"sleeping {sleep_for}s")
            sleeper(sleep_for)

    return state


def describe(state: WatchState) -> str:
    lines = [
        f"watch stopped: {state.stopped_reason or 'unknown'}",
        f"  passes    : {state.iterations}",
        f"  launched  : {state.launched}",
        f"  resumed   : {state.woken}",
    ]
    if state.last_report:
        lines.append("  last pass :")
        lines.extend(f"    {line}" for line in state.last_report.splitlines())
    return "\n".join(lines)
