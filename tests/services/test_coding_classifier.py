"""Tests for the coding-classifier judge prompt and response schema."""

from qfa.services.coding_classifier import _JUDGE_SYSTEM, JudgeResponse


def test_judge_system_limits_explanation_to_two_sentences():
    """The judge system prompt caps the per-level explanation at two sentences.

    Why: the assign-codes explanation is built by concatenating one judge
    explanation per hierarchy level (see ``Orchestrator._ScoredCode.explanation``);
    an unbounded per-level explanation made the combined result too long.
    """
    assert "at most two sentences" in _JUDGE_SYSTEM


def test_judge_response_explanation_field_documents_the_limit():
    """The structured-output schema also documents the two-sentence cap.

    Why: field descriptions on the ``response_model`` reach the LLM as part
    of the structured-output schema, so this is a second, independent
    reinforcement of the same constraint communicated in the system prompt.
    """
    field = JudgeResponse.model_fields["explanation"]
    assert "two sentences" in field.description
