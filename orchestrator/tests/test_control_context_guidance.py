"""Controls prefer bounded reads without losing independent review or intent."""
from agentkit import instructions


def test_coordinator_uses_compact_intent_and_recovery_receipts(project):
    text = instructions.role_text('coordinator', project)
    assert 'include_history=False' in text
    assert 'include_report=False' in text
    assert 'model_pins' in text
    assert 'specific unresolved finding' in text


def test_reviewer_reuses_authoritative_brief_before_fetching_job_history(project):
    text = instructions.role_text('reviewer', project)
    assert 'brief.job_memory' in text
    assert 'include_history=False' in text
    assert 'independent' in text.lower()
    assert 'complete diff' in text
