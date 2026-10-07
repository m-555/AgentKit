"""Regressions found by piloting AgentKit on a real repository.

Each class here pins one defect that a green test suite did not catch, because
every one of them lives at a seam: between the config `init` writes and the shell
that has to run it, between two modules that were each individually reasonable,
or between a command's default value and the registry that has to honour it.

The suite passed 340 tests with all of these present. What they have in common is
that none of them could be seen without running the tool end to end on a real
repository, on Windows, with a real worker.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import adapters, cli, db, gates, hooks_cli, operator, overlap, recovery
from agentkit import statemachine as sm
from agentkit.config import load_project
from agentkit.console import use_utf8
from agentkit.init_project import detect, init
from tests.conftest import commit_all, git


class TestGateCommandsAreExecutable:
    """`init` wrote a command the shell it runs under could not execute.

    `gates._run_one` uses `shell=True`, which on Windows means cmd.exe. cmd reads
    the leading token of `.venv/Scripts/python.exe` up to the first `/` and reports
    "'.venv' is not recognized as an internal or external command", so onboarding
    failed at the first gate on every Windows Python project.
    """

    def _venv_project(self, tmp_path: Path) -> Path:
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0"\n', encoding="utf-8"
        )
        # Both layouts, so the assertion holds whichever one `detect` picks.
        for rel in ("Scripts/python.exe", "bin/python"):
            target = root / ".venv" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        return root

    def test_generated_interpreter_path_is_quoted(self, tmp_path: Path) -> None:
        found = detect(self._venv_project(tmp_path))
        commands = [c for level in found.gates.values() for c in level]
        assert commands, "a project with tests should get gate commands"
        venv_commands = [c for c in commands if ".venv" in c]
        assert venv_commands, "this fixture has a .venv, so it should be used"
        for command in venv_commands:
            assert command.startswith('"'), (
                f"{command!r} must quote the interpreter path: an unquoted "
                "forward-slash path is not executable by cmd.exe"
            )

    def test_generated_gate_actually_runs(self, tmp_path: Path) -> None:
        """The real proof: hand the generated string to the same shell gates use.

        A real venv, not a stand-in binary — the bug was that the *shell* could not
        resolve the path, so anything short of asking the shell to resolve it is
        testing something else.
        """
        root = tmp_path / "real"
        root.mkdir()
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0"\n', encoding="utf-8"
        )
        made = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", ".venv"],
            cwd=str(root), capture_output=True, text=True, timeout=300,
        )
        if made.returncode != 0:
            pytest.skip(f"could not create a venv here: {made.stderr.strip()}")

        found = detect(root)
        command = next(c for level in found.gates.values() for c in level if ".venv" in c)
        interpreter = command.split(" -m ")[0]
        runnable = f"{interpreter} -c \"print('ok')\""
        proc = subprocess.run(
            runnable, shell=True, cwd=str(root), capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, (
            f"generated gate command is not executable by the shell gates use:\n"
            f"  {runnable}\n  {(proc.stderr or proc.stdout).strip()}"
        )
        assert "ok" in proc.stdout


class TestWorktreeEnvironment:
    """A worker's gate could never pass, and nothing said so.

    `init` pointed gates at `.venv`; `worktrees` deliberately never puts one in a
    worktree. Each decision is right alone. Together they guaranteed that every
    Python worker failed `gate_run`, and a failed gate increments `attempts`, so
    three of them pushed a healthy task to NEEDS_REPLAN.
    """

    def test_detector_flags_a_gate_that_cannot_run_in_a_worktree(
        self, project_root: Path
    ) -> None:
        (project_root / ".ai" / "project.yaml").write_text(
            "name: demo\nstacks: [python]\n"
            'gates:\n  fast: [".venv/bin/python -m pytest -q"]\n',
            encoding="utf-8",
        )
        problems = gates.unrunnable_in_worktree(load_project(project_root))
        assert len(problems) == 1
        level, command, reason = problems[0]
        assert level == "fast"
        assert ".venv" in command
        assert "worktree" in reason

    def test_declaring_worktree_setup_clears_the_warning(self, project_root: Path) -> None:
        (project_root / ".ai" / "project.yaml").write_text(
            "name: demo\nstacks: [python]\n"
            'gates:\n  fast: [".venv/bin/python -m pytest -q"]\n'
            'worktree_setup: ["python -m venv .venv"]\n',
            encoding="utf-8",
        )
        assert gates.unrunnable_in_worktree(load_project(project_root)) == []

    def test_a_worktree_free_gate_is_not_flagged(self, project: object) -> None:
        assert gates.unrunnable_in_worktree(project) == []

    def test_init_generates_setup_when_it_generates_a_venv_gate(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0"\n', encoding="utf-8"
        )
        for rel in ("Scripts/python.exe", "bin/python"):
            target = root / ".venv" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")

        init(root)
        config = load_project(root)
        assert config.worktree_setup, (
            "a generated .venv gate without generated setup is the bug: the worker "
            "would fail a gate it had no way to pass"
        )
        assert gates.unrunnable_in_worktree(config) == []

    def _py_project(self, tmp_path: Path, pyproject: str) -> Path:
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
        for rel in ("Scripts/python.exe", "bin/python"):
            target = root / ".venv" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        return root

    def test_setup_installs_the_extra_that_holds_pytest(self, tmp_path: Path) -> None:
        """A base install almost never carries pytest.

        Without this the worktree builds cleanly and then cannot run one gate —
        which is the original bug wearing a different hat.
        """
        root = self._py_project(
            tmp_path,
            '[project]\nname = "p"\nversion = "0"\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0", "ruff"]\n',
        )
        found = detect(root)
        install = [c for c in found.setup if "pip install" in c]
        assert install and "[dev]" in install[0], found.setup

    def test_setup_finds_a_pytest_extra_under_an_unconventional_name(
        self, tmp_path: Path
    ) -> None:
        root = self._py_project(
            tmp_path,
            '[project]\nname = "p"\nversion = "0"\n'
            "[project.optional-dependencies]\n"
            'checks = ["pytest>=8.0"]\n',
        )
        found = detect(root)
        install = [c for c in found.setup if "pip install" in c]
        assert install and "[checks]" in install[0], found.setup

    def test_no_extra_installs_plainly_and_warns(self, tmp_path: Path) -> None:
        root = self._py_project(tmp_path, '[project]\nname = "p"\nversion = "0"\n')
        found = detect(root)
        install = [c for c in found.setup if "pip install" in c]
        assert install and "[" not in install[0]
        assert any("extra" in note for note in found.notes), (
            "guessing wrong here is silent, so it has to be said out loud"
        )

    def test_setup_runs_in_the_given_directory(self, project_root: Path, tmp_path: Path) -> None:
        (project_root / ".ai" / "project.yaml").write_text(
            "name: demo\nstacks: [python]\n"
            'worktree_setup: ["python -c \\"open(\'built.txt\',\'w\').write(\'1\')\\""]\n',
            encoding="utf-8",
        )
        elsewhere = tmp_path / "somewhere"
        elsewhere.mkdir()
        result = gates.run_worktree_setup(load_project(project_root), elsewhere)
        assert result.passed, result.summary()
        assert (elsewhere / "built.txt").is_file(), "setup must run in the worktree"

    def test_failing_setup_is_reported_not_swallowed(self, project_root: Path) -> None:
        (project_root / ".ai" / "project.yaml").write_text(
            "name: demo\nstacks: [python]\n"
            'worktree_setup: ["python -c \\"raise SystemExit(3)\\""]\n',
            encoding="utf-8",
        )
        result = gates.run_worktree_setup(load_project(project_root), project_root)
        assert not result.passed
        assert "worktree_setup" in result.summary()


class TestAdapterNaming:
    """`agentkit launch` had no working `--agent` value.

    The default was `claude`, which no adapter answered to; the real name
    `claude-code` was rejected by argparse's `choices`. Two spellings in two
    places, neither aware of the other.
    """

    def test_short_name_resolves_to_the_adapter(self) -> None:
        adapter = adapters.get("claude")
        assert adapter is not None, "`claude` is what a person types"
        assert adapter.name == "claude-code"

    def test_canonical_name_is_unchanged_for_real_names(self) -> None:
        assert adapters.canonical_name("codex") == "codex"
        assert adapters.canonical_name("claude-code") == "claude-code"

    def test_unknown_name_still_returns_none(self) -> None:
        assert adapters.get("not-an-agent") is None

    @pytest.mark.parametrize("name", ["claude", "claude-code", "codex"])
    def test_every_offered_choice_resolves(self, name: str) -> None:
        """The defect in one line: a `--agent` value the registry cannot honour."""
        assert name in adapters.selectable_names()
        assert adapters.get(name) is not None

    def test_launch_default_is_a_resolvable_adapter(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["launch", "1"])
        assert adapters.get(args.agent) is not None, (
            f"`agentkit launch` defaults to {args.agent!r}, which no adapter answers to"
        )


class TestDeferralReasons:
    """Running out of worker slots was reported as a predicted file conflict.

    `agentkit run` said "predicted overlap with running or selected work" for a
    task that overlapped nothing, while `agentkit why serialized` said it had not
    been serialised at all — because no serialisation event was recorded, correctly.
    The two commands contradicted each other and sent you hunting a phantom.
    """

    def test_capacity_deferral_says_capacity(self, conn, project_root: Path, make_task) -> None:
        a = make_task("a", ["services/media.py"], status=sm.READY)
        b = make_task("b", ["services/retry.py"], status=sm.READY)
        tasks = [db.get_task(conn, a), db.get_task(conn, b)]

        chosen, deferred = overlap.schedulable_set(conn, project_root, tasks, limit=1)
        assert len(chosen) == 1
        assert len(deferred) == 1
        _task, reason = deferred[0]
        assert "worker limit" in reason
        assert "no path conflict" in reason
        assert "overlap" not in reason.lower(), (
            "a capacity deferral must not be described as a predicted overlap"
        )

    def test_capacity_deferral_records_no_serialization_event(
        self, conn, project_root: Path, make_task
    ) -> None:
        """What made the contradiction visible: `why serialized` reads this log."""
        a = make_task("a", ["services/media.py"], status=sm.READY)
        b = make_task("b", ["services/retry.py"], status=sm.READY)
        tasks = [db.get_task(conn, a), db.get_task(conn, b)]
        overlap.schedulable_set(conn, project_root, tasks, limit=1)

        events = db.recent_events(conn, task_id=b, kind="serialization_decision")
        assert events == [], "no serialisation happened, so none should be claimed"

    def test_real_overlap_still_says_overlap_and_names_the_blocker(
        self, conn, project_root: Path, make_task
    ) -> None:
        held = make_task("held", ["services/media.py"], status=sm.RUNNING)
        want = make_task("want", ["services/media.py"], status=sm.READY)
        chosen, deferred = overlap.schedulable_set(
            conn, project_root, [db.get_task(conn, want)], limit=5
        )
        assert chosen == []
        _task, reason = deferred[0]
        assert str(held) in reason, "the deferral should name what it is waiting on"
        assert "worker limit" not in reason


class TestGateRunsWhereTheCallerStands:
    """`agentkit gate` inside a worktree silently tested the main checkout.

    `find_project_root` resolves a linked worktree back to the main checkout on
    purpose — that is where `tasks.db` must live. Passing that as the gate's `cwd`
    meant a human reviewing a worker's branch got a result for entirely different
    code. The MCP `gate_run` tool never had this bug.
    """

    def test_gate_uses_the_current_directory(
        self, project_root: Path, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        (project_root / ".ai" / "project.yaml").write_text(
            "name: demo\nstacks: [python]\n"
            'gates:\n  fast: ["python -c \\"import pathlib,os;'
            ' pathlib.Path(\'where.txt\').write_text(os.getcwd())\\""]\n',
            encoding="utf-8",
        )
        commit_all(project_root, "gate that records its cwd")
        worktree = tmp_path / "wt-demo"
        git(project_root, "worktree", "add", "-q", "-b", "agent/x", str(worktree))

        monkeypatch.chdir(worktree)
        parser = cli.build_parser()
        args = parser.parse_args(["gate", "fast"])
        assert args.func(args) == 0
        capsys.readouterr()

        assert (worktree / "where.txt").is_file(), (
            "the gate ran somewhere other than the directory the caller was in"
        )
        assert not (project_root / "where.txt").exists(), (
            "the gate ran in the main checkout instead of the worktree under review"
        )


class TestOperatorLeaseLeavesNothingBehind:
    """Claim, edit, release — and every cycle left litter in the task list.

    `release` freed the leases but left the singleton RUNNING with no worker
    process, so the next `reconcile` called it STALE and asked a human to adopt or
    discard a task that had simply finished. `discard` then reset it to READY,
    putting a human's abandoned claim into the agent work queue, where — having no
    capability requirements of its own — it was a candidate for a real worker.
    """

    def test_release_stands_the_task_down(self, conn) -> None:
        operator.acquire(conn, ["docs/**"], reason="manual edit")
        task_id = operator.ensure_task(conn)
        assert db.get_task(conn, task_id)["status"] == sm.RUNNING

        operator.release(conn)
        status = str(db.get_task(conn, task_id)["status"])
        assert status not in sm.ACTIVE, (
            f"a released operator claim is still {status}; reconcile will call it STALE"
        )
        assert status == sm.CANCELLED

    def test_release_frees_the_paths(self, conn) -> None:
        operator.acquire(conn, ["docs/**"], reason="manual edit")
        operator.release(conn)
        assert db.active_leases(conn) == []

    def test_the_singleton_is_reusable_after_release(self, conn) -> None:
        first = operator.acquire(conn, ["docs/**"]).task_id
        operator.release(conn)
        again = operator.acquire(conn, ["README.md"])
        assert again.ok, again.summary()
        assert again.task_id == first, "the operator task is a reused singleton"
        assert db.get_task(conn, first)["status"] == sm.RUNNING

    def test_discarding_an_operator_task_does_not_queue_it(
        self, conn, project_root: Path
    ) -> None:
        operator.acquire(conn, ["docs/**"])
        task_id = operator.ensure_task(conn)
        db.set_status(conn, task_id, sm.STALE, actor="scheduler", cause="test")

        recovery.discard(conn, project_root, task_id)
        status = str(db.get_task(conn, task_id)["status"])
        assert status == sm.CANCELLED, (
            f"a discarded operator claim became {status}; READY would offer a "
            "human's abandoned lease claim to the scheduler as agent work"
        )

    def test_discarding_an_ordinary_task_still_requeues_it(
        self, conn, project_root: Path, make_task
    ) -> None:
        task_id = make_task("real work", ["services/media.py"], status=sm.STALE)
        recovery.discard(conn, project_root, task_id)
        assert db.get_task(conn, task_id)["status"] == sm.READY


class TestTaskCancel:
    """There was no way to remove a task from the CLI at all."""

    def test_cancel_stops_the_task_and_frees_its_paths(
        self, conn, project_root: Path, make_task, capsys
    ) -> None:
        task_id = make_task("wrong task", ["services/media.py"], status=sm.RUNNING)
        db.try_acquire_leases(conn, task_id, ["services/media.py"], mode="exclusive-write")
        conn.commit()

        parser = cli.build_parser()
        args = parser.parse_args(["--path", str(project_root), "task", "cancel", str(task_id)])
        assert args.func(args) == 0
        capsys.readouterr()

        fresh = db.connect(project_root)
        try:
            assert fresh.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0] == sm.CANCELLED
            assert [
                lease for lease in db.active_leases(fresh)
                if int(lease["task_id"]) == task_id
            ] == []
        finally:
            fresh.close()

    def test_cancel_refuses_from_a_state_the_machine_forbids(
        self, conn, project_root: Path, make_task, capsys
    ) -> None:
        task_id = make_task("nearly done", ["services/media.py"], status=sm.RUNNING)
        db.set_status(conn, task_id, sm.VERIFYING, actor="scheduler", cause="test")
        conn.commit()

        parser = cli.build_parser()
        args = parser.parse_args(["--path", str(project_root), "task", "cancel", str(task_id)])
        assert args.func(args) == 1
        assert "cannot be cancelled" in capsys.readouterr().err


class TestHookFailsOpenLoudly:
    """A malformed payload disabled enforcement for that call, in silence.

    Failing open is the right default — a hook bug must not wedge a session — but
    the module docstring promises it happens *loudly*, and a JSON decode error
    produced no output at all. An enforcement layer could be off for a whole run
    with nothing to show for it.
    """

    def _read_with_stdin(self, monkeypatch, capsys, raw: str) -> tuple[dict, str]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        payload = hooks_cli._read_input()
        return payload, capsys.readouterr().err

    def test_malformed_json_warns(self, monkeypatch, capsys) -> None:
        # The authentic case: a Windows path whose single backslashes were never
        # escaped, so `\p` is not a legal JSON escape.
        payload, err = self._read_with_stdin(
            monkeypatch, capsys, r'{"cwd": "E:\projects\demo", "tool_name": "Write"}'
        )
        assert payload == {}
        assert "AgentKit" in err
        assert "not valid JSON" in err

    def test_non_object_payload_warns(self, monkeypatch, capsys) -> None:
        payload, err = self._read_with_stdin(monkeypatch, capsys, "[1, 2, 3]")
        assert payload == {}
        assert "expected an object" in err

    def test_empty_stdin_is_silent(self, monkeypatch, capsys) -> None:
        """Several hook events legitimately send nothing; that is not a fault."""
        payload, err = self._read_with_stdin(monkeypatch, capsys, "")
        assert payload == {}
        assert err == ""

    def test_valid_payload_is_silent(self, monkeypatch, capsys) -> None:
        payload, err = self._read_with_stdin(
            monkeypatch, capsys, json.dumps({"tool_name": "Write"})
        )
        assert payload == {"tool_name": "Write"}
        assert err == ""


class TestConsoleEncoding:
    """Em dashes reached the console — and the model — as U+FFFD on Windows.

    Cosmetic in `doctor`. Not cosmetic in `_block`, which writes the refusal the
    model reads.
    """

    def test_use_utf8_is_safe_to_call_repeatedly(self) -> None:
        use_utf8()
        use_utf8()

    def test_use_utf8_survives_a_stream_without_reconfigure(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        use_utf8()          # must not raise on a stream with no reconfigure()

    def test_block_message_round_trips_an_em_dash(self) -> None:
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
        message = "blocked — stay in scope"
        stream.write(message)
        stream.flush()
        written = stream.buffer.getvalue().decode("utf-8")
        assert "—" in written and "\ufffd" not in written
