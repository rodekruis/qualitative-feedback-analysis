"""Tests for ubiquitous language in LLM system prompts."""

from qfa.services import coding_classifier, orchestrator


def test_orchestrator_system_prompts_use_ubiquitous_language():
    assert "beneficiary" not in orchestrator._SYSTEM_MESSAGE_TEMPLATE
    assert "beneficiary" not in orchestrator._DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT
    assert "documents" not in orchestrator._SYSTEM_MESSAGE_TEMPLATE
    assert (
        "feedback records from community members"
        in orchestrator._SYSTEM_MESSAGE_TEMPLATE
    )
    assert (
        "feedback records from community members"
        in orchestrator._DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT
    )


def test_coding_classifier_system_prompt_uses_ubiquitous_language():
    assert "beneficiary" not in coding_classifier.SYSTEM_PROMPT
    assert "feedback records from community members" in coding_classifier.SYSTEM_PROMPT
