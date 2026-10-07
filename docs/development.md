# Develop and verify AgentKit

Work in the AgentKit source checkout; install its orchestrator package with the
dev extra. Run focused checks for the change and the required integration gates.
A documentation-only change needs link, skill and prompt checks, not repeated
full suites or real model requests.

## Keep disposable Git fixtures out of projects

Tests create demo repositories and worktrees. Put pytest's temporary directory
outside both the AgentKit source checkout and the target project, on a short path
such as `C:/agentkit-test-runtime`; use a unique run directory so parallel runs
never delete or reuse each other's fixtures. On Windows, keep that path short: deep
temporary paths can exceed path-length limits and make Git-based tests fail.

```powershell
$agentkitRun = Join-Path 'C:/agentkit-test-runtime' ('pytest-' + [guid]::NewGuid().ToString('N'))
uv run pytest -q --basetemp $agentkitRun
uv run ruff check agentkit tests
uv run mypy agentkit
```

Run these from `orchestrator/`. Any short temporary root outside the repositories
works; on Linux or macOS use a directory under `/tmp`.
Never aim `--basetemp` at a repository, a real worker worktree or a parent of
user data: pytest treats its chosen run directory as disposable.

Functional probes may use an external `fixture_root` when called from Python.
Keep their capability receipts and manager checkpoints in the project's runtime
folder. Static metadata checks make no inference requests; real functional
certification is separate and must be explicitly authorized. Skill validation
and assembled-prompt checks do not establish a model's task quality or quota wake.
