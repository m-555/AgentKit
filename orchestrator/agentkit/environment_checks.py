"""Project-declared readiness checks run on the host, never inside an AI task."""
from . import gates, worktrees


def run(project, work, profile):
    import os
    env = {**os.environ, **worktrees.shared_cache_env(project.root)}
    results = []
    for command in profile.get("checks", []):
        result = gates._run_one(command, work, gates.DEFAULT_TIMEOUT, env=env)
        results.append(result)
        if not result.ok:
            break
    return gates.GateResult("environment_checks", all(r.ok for r in results), results)
