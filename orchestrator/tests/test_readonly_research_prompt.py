"""Read-only local assessment must not receive a source-writing task packet."""
from agentkit import instructions


def test_research_prompt_does_not_inherit_builder_or_tester_work(project):
    project.raw["workflow"] = {"mode": "separate-tasks"}
    task = {"kind": "RESEARCH", "role": "backend-tester", "expected_write": [],
            "skills": []}
    text = instructions.prompt(task, project, "COMMIT BEFORE GATE")
    assert "COMMIT BEFORE GATE" not in text
    assert "read-only research" in text
    assert "Do not write files" in text
    assert "task_status REVIEW" in text
