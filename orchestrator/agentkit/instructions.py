"""Explicit role and skill delivery; independent of plugin discovery."""
from pathlib import Path


def role_text(role: str, project) -> str:
    from . import workflow
    separated = workflow.role_text(project, role)
    if separated is not None:
        return separated
    custom = (getattr(project, "raw", {}).get("roles") or {}).get(role)
    if custom:
        return project_file(project.root, custom)
    path = Path(__file__).resolve().parents[2] / "plugins" / "agentkit" / "agents" / f"{role}.md"
    if not path.is_file():
        path = Path(__file__).parent / "data" / "agents" / f"{role}.md"
    if not path.is_file():
        raise ValueError(f"role {role!r} has no instructions; configure roles.{role}")
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        content = content.split("---", 2)[-1]
    return content.strip()


def project_file(root, relative: str) -> str:
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"missing or escaping instruction file: {relative}")
    return path.read_text(encoding="utf-8")


def prompt(task, project, base: str, *, role=None) -> str:
    role = role or task.get("role", "implementer")
    if task.get("kind") == "RESEARCH" and not task.get("expected_write"):
        parts = [
            "You are a read-only research worker for one bounded task. First call "
            "AgentKit brief. Read only the assigned files. Do not write files, run "
            "commands/tests, install dependencies, invoke task_commit or delegate. "
            "Save concise findings with AgentKit checkpoint, then task_status REVIEW. "
            "The manager runs any declared checks and reviews the findings. "
            "If a required tool is denied or unavailable, report BLOCKED and stop.",
        ]
    else:
        parts = [role_text(role, project), base]
    from . import workflow
    control = role in ("coordinator", "reviewer", "architect")
    memory = "" if workflow.enabled(project) and control else workflow.job_context(project, task.get("job_id"), task)
    if memory and not workflow.enabled(project):
        parts.append(memory)  # Separate mode delivers intent once, through brief().
    for relative in task.get("skills") or []:
        builtin = Path(__file__).resolve().parents[2] / "plugins" / "agentkit" / "skills"
        if not builtin.is_dir():
            builtin = Path(__file__).parent / "data" / "skills"
        if "/" not in relative and "\\" not in relative and (builtin / relative / "SKILL.md").is_file():
            content = project_file(builtin, relative + "/SKILL.md")
        else:
            content = project_file(project.root, relative)
        parts.append(f"## Skill: {relative}\n" + content)
    from .commit_format import worker_note
    note = worker_note(project)
    if note:
        parts.append(note)
    result = "\n\n".join(parts)
    workflow.validate_prompt(project, result)
    return result
