"""Tests for the one-shot coding-classifier prompt, flattening, and schema."""

from qfa.domain.models import CodingNode
from qfa.services.coding_classifier import (
    SYSTEM_PROMPT,
    CodeSelection,
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


def test_system_prompt_limits_explanation_to_two_sentences():
    """Caps the per-selection explanation so combined rejected explanations stay short."""
    assert "two sentences" in SYSTEM_PROMPT


def test_code_selection_confidence_has_no_schema_level_bound():
    """No ge/le constraint.

    An out-of-range score must reach the orchestrator as a parsed value (not
    a validation failure) so it can raise the domain-specific
    ``AnalysisError`` instead of a generic parse error.
    """
    field = CodeSelection.model_fields["confidence"]
    assert field.metadata == []


def test_code_selection_explanation_field_documents_the_limit():
    field = CodeSelection.model_fields["explanation"]
    assert "two sentences" in field.description
