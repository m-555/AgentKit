"""F15: a failed gate shows why it failed, not only the end of its output.

Gate output was cut to its last 4000 characters and the summary to 25 lines.
pytest prints its FAILURES section before the warnings and the short summary,
and Playwright prints the failing expectation before the web-server log, so
pilot workers saw only warnings or build logs: task 74 spent its 32 tool calls
on that, and task 75 blocked on a KeyError it could not see. The failure lines
are now extracted from the full output before it is truncated.
"""
from __future__ import annotations

import sys

from agentkit import gates
from agentkit.gate_excerpt import failure_excerpt

PYTEST = """\
============================= test session starts =============================
collected 40 items
tests\\creative_workflows\\test_release_preservation.py ....F........ [100%]
================================== FAILURES ===================================
____ test_plan_is_deterministic_unapproved_and_binds_only_selected_stills _____
tests\\creative_workflows\\test_release_preservation.py:202: in test_plan_is_deterministic
    prepared = {s["step_id"]: s for s in plan["prepared_steps"]}
E   KeyError: 'prepared_steps'
""" + "routes\\x.py:64: PydanticDeprecatedSince20: Pydantic V1 style `@validator` is deprecated.\n" * 80 + """\
=========================== short test summary info ===========================
FAILED tests/creative_workflows/test_release_preservation.py::test_plan_is_deterministic_unapproved_and_binds_only_selected_stills
================== 1 failed, 39 passed, 9 warnings in 2.97s ===================
"""

PLAYWRIGHT = """\
Running 3 tests using 1 worker
  \x1b[31m\u2718\x1b[39m  2 [chromium] \u203a creative.spec.ts:41:3 \u203a Recipes tab mounts lazily (5.1s)
  1) [chromium] \u203a creative.spec.ts:41:3 \u203a Recipes tab mounts lazily
    Error: expect(locator).toBeVisible() failed
    Locator: getByRole('tab', { name: 'Recipes' })
    Expected: visible
    Received: <element(s) not found>
""" + "[WebServer] vite v6 building for production... transforming (412) src/main.jsx\n" * 120


def test_pytest_failure_lines_survive_a_long_warning_tail():
    excerpt = failure_excerpt(PYTEST)
    assert "E   KeyError: 'prepared_steps'" in excerpt
    assert "test_release_preservation.py:202: in test_plan_is_deterministic" in excerpt
    assert "FAILED tests/creative_workflows/test_release_preservation.py::test_plan_is_deterministic" in excerpt
    assert "PydanticDeprecatedSince20" not in excerpt


def test_a_playwright_expectation_survives_the_web_server_log():
    excerpt = failure_excerpt(PLAYWRIGHT)
    assert "Error: expect(locator).toBeVisible() failed" in excerpt
    assert "Received: <element(s) not found>" in excerpt and "\x1b[" not in excerpt
    assert "[WebServer]" not in excerpt


def test_the_excerpt_is_bounded():
    noisy = "E   AssertionError: assert 1 == 2\n" * 5000
    excerpt = failure_excerpt(noisy)
    assert len(excerpt.splitlines()) <= 60 and len(excerpt) <= 6000


def test_a_failed_gate_summary_leads_with_the_failure(project_root):
    script = project_root / "fail.py"
    script.write_text("import sys\nsys.stdout.write(" + repr(PYTEST) + ")\nsys.exit(1)\n", encoding="utf-8")
    result = gates._run_one(f'"{sys.executable}" fail.py', project_root, 60)
    assert "KeyError: 'prepared_steps'" not in result.output_tail  # truncated away, as before
    summary = gates.GateResult("creative", False, [result]).summary()
    assert "E   KeyError: 'prepared_steps'" in summary
    assert summary.index("KeyError") < summary.index("1 failed, 39 passed")
    assert gates.GateResult("creative", False, [result]).to_dict()["commands"][0]["failure_excerpt"]


def test_a_passing_command_keeps_no_excerpt(project_root):
    result = gates._run_one(f'"{sys.executable}" -c "print(1)"', project_root, 60)
    assert result.ok and result.failure_excerpt == ""
    assert gates.GateResult("fast", True, [result]).summary() == "gate `fast` passed (1 command(s))"
