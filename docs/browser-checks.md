# Private Playwright checks

Browser gates must test the assigned worktree, not a reused server from another checkout.
Opt in through project-owned `.ai/project.yaml`:

```yaml
playwright_checks:
  - match_command: test:e2e
    config: apps/web/playwright.config.js
    server_command: npm run dev -- --host 127.0.0.1 --port {port} --strictPort
```

AgentKit creates a host-owned runtime config importing that worktree's actual config.
It selects a loopback port, resolves the original test directory, overrides baseURL
and server command/cwd, and sets reuseExistingServer false. Port conflicts fail;
the host does not kill an existing developer server. Playwright owns startup and cleanup.
Test assertions, retry policy, projects and matching stay inherited. Test outputs go
under ignored runtime/browser-checks. Commands must be single commands without a
pre-existing --config override. Config files cannot resolve outside the worktree.
Nonmatching commands and projects without this setting retain their existing behavior.
Changing the recipe invalidates cached verification. This opt-in adapter supports
an ordinary single-server Playwright configuration; other runners need their own adapter.
