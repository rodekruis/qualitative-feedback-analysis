"""Tests for the one-shot pick prompt/schema and the per-level judge prompt/schema."""

from qfa.domain.models import CodingNode
from qfa.services.coding_classifier import (
    _JUDGE_SYSTEM,
    SYSTEM_PROMPT,
    CodingResponse,
    JudgeResponse,
    build_coding_messages,
    flatten_coding_nodes,
)


def _make_feedback_record():
    from qfa.domain.models import FeedbackRecordModel

    return FeedbackRecordModel(id="fb-1", content="Long waiting times at the clinic.")


class TestFlattenCodingNodes:
    def test_every_node_becomes_its_own_option_at_every_depth(self):
        """Non-leaf nodes are selectable too, not just leaves."""
        nodes = [
            CodingNode(
                id="type-a",
                name="Type A",
                children=[
                    CodingNode(
                        id="cat-a1",
                        name="Cat A1",
                        children=[CodingNode(id="code-a1-1", name="Code A1.1")],
                    )
                ],
            )
        ]
        options = flatten_coding_nodes(nodes)
        labels = [opt.label for opt in options]
        assert labels == [
            "Type A",
            "Type A > Cat A1",
            "Type A > Cat A1 > Code A1.1",
        ]

    def test_multiple_root_branches_are_each_flattened(self):
        nodes = [
            CodingNode(id="a", name="A", children=[CodingNode(id="a1", name="A1")]),
            CodingNode(id="b", name="B"),
        ]
        options = flatten_coding_nodes(nodes)
        assert [opt.label for opt in options] == ["A", "A > A1", "B"]

    def test_path_carries_ids_alongside_names(self):
        nodes = [CodingNode(id="type-a", name="Type A")]
        options = flatten_coding_nodes(nodes)
        assert options[0].path == (("type-a", "Type A"),)


class TestBuildCodingMessages:
    def test_no_options_returns_empty_user_message(self):
        system, user = build_coding_messages(
            feedback_record=_make_feedback_record(), options=[]
        )
        assert system == SYSTEM_PROMPT
        assert user == ""

    def test_options_are_numbered_in_the_user_message(self):
        nodes = [
            CodingNode(id="a", name="Code A"),
            CodingNode(id="b", name="Code B"),
        ]
        _, user = build_coding_messages(
            feedback_record=_make_feedback_record(),
            options=flatten_coding_nodes(nodes),
        )
        assert "0: Code A" in user
        assert "1: Code B" in user

    def test_user_message_includes_the_feedback_text(self):
        _, user = build_coding_messages(
            feedback_record=_make_feedback_record(),
            options=flatten_coding_nodes([CodingNode(id="a", name="Code A")]),
        )
        assert "Long waiting times at the clinic." in user


def test_pick_response_has_no_confidence_or_explanation():
    """The pick step only selects indices; scoring is the judge's job.

    Why: confidence and explanation moved to the separate per-level judge
    call, so the pick schema carries neither — asking the model for them
    twice would be redundant and could disagree with the judge's own score.
    """
    assert set(CodingResponse.model_fields) == {"selected"}


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
