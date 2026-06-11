"""Tests for API schemas."""

import logging

import pytest
from pydantic import ValidationError

from qfa.api.schemas import (
    ApiAssignedCode,
    ApiCodingFramework,
    ApiCodingNode,
    ApiSummarizeBulkResponse,
    _create_pretty_output,
    _resolve_language,
)


def test_max_child_depth_leaf_returns_zero():
    assert ApiCodingNode(id="a", name="a").max_child_depth() == 0


def test_max_child_depth_single_level():
    node = ApiCodingNode(
        id="root", name="root", children=[ApiCodingNode(id="child", name="child")]
    )
    assert node.max_child_depth() == 1


def test_max_child_depth_two_levels():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(
                id="mid", name="mid", children=[ApiCodingNode(id="leaf", name="leaf")]
            )
        ],
    )
    assert node.max_child_depth() == 2


def test_max_child_depth_returns_deepest_branch():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(id="shallow", name="shallow"),
            ApiCodingNode(
                id="deep",
                name="deep",
                children=[ApiCodingNode(id="deeper", name="deeper")],
            ),
        ],
    )
    assert node.max_child_depth() == 2


def test_min_child_depth_leaf_returns_zero():
    assert ApiCodingNode(id="a", name="a").min_child_depth() == 0


def test_min_child_depth_single_level():
    node = ApiCodingNode(
        id="root", name="root", children=[ApiCodingNode(id="child", name="child")]
    )
    assert node.min_child_depth() == 1


def test_min_child_depth_two_levels():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(
                id="mid", name="mid", children=[ApiCodingNode(id="leaf", name="leaf")]
            )
        ],
    )
    assert node.min_child_depth() == 2


def test_min_child_depth_returns_shallowest_branch():
    node = ApiCodingNode(
        id="root",
        name="root",
        children=[
            ApiCodingNode(id="shallow", name="shallow"),
            ApiCodingNode(
                id="deep",
                name="deep",
                children=[ApiCodingNode(id="deeper", name="deeper")],
            ),
        ],
    )
    assert node.min_child_depth() == 1


def test_coding_levels_valid_3_levels_enforced():
    """Flat tree (depth 0) must now have 3 levels. Test the new minimum requirement."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="a", name="a"),
                ApiCodingNode(id="b", name="b"),
            ]
        )


def test_coding_levels_valid_uniform_depth():
    levels = ApiCodingFramework(
        root_codes=[
            ApiCodingNode(
                id="water-1",
                name="Water",
                children=[
                    ApiCodingNode(
                        id="dist-1",
                        name="Distribution",
                        children=[ApiCodingNode(id="wait-1", name="Waiting times")],
                    )
                ],
            ),
            ApiCodingNode(
                id="health-1",
                name="Health",
                children=[
                    ApiCodingNode(
                        id="staff-1",
                        name="Staff",
                        children=[ApiCodingNode(id="supplies-1", name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2


def test_coding_levels_unequal_depth_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="flat", name="flat"),
                ApiCodingNode(
                    id="deep",
                    name="deep",
                    children=[ApiCodingNode(id="child", name="child")],
                ),
            ]
        )


def test_coding_levels_unequal_depth_within_subtree_raises():
    with pytest.raises(ValueError, match="same depth"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(
                    id="root",
                    name="root",
                    children=[
                        ApiCodingNode(id="shallow", name="shallow"),
                        ApiCodingNode(
                            id="deep",
                            name="deep",
                            children=[ApiCodingNode(id="deeper", name="deeper")],
                        ),
                    ],
                ),
            ]
        )


def test_coding_levels_with_no_children_fail():
    with pytest.raises(ValidationError, match="should have at least 1"):
        ApiCodingFramework(root_codes=[])


def test_coding_node_missing_id_raises():
    """ApiCodingNode requires id field."""
    with pytest.raises(ValidationError, match="id"):
        ApiCodingNode(name="test")  # type: ignore


def test_coding_levels_depth_less_than_3_raises():
    """Coding framework must have exactly 3 levels (depth=2). Depth < 2 should raise."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(id="type-1", name="Type A"),
                ApiCodingNode(id="type-2", name="Type B"),
            ]
        )


def test_coding_levels_depth_more_than_3_raises():
    """Coding framework must have exactly 3 levels (depth=2). Depth > 2 should raise."""
    with pytest.raises(ValueError, match="exactly 3 levels"):
        ApiCodingFramework(
            root_codes=[
                ApiCodingNode(
                    id="type-1",
                    name="Type A",
                    children=[
                        ApiCodingNode(
                            id="cat-1",
                            name="Category A",
                            children=[
                                ApiCodingNode(
                                    id="code-1",
                                    name="Code A",
                                    children=[
                                        ApiCodingNode(
                                            id="extra-1",
                                            name="Extra Level",
                                        )
                                    ],
                                )
                            ],
                        )
                    ],
                )
            ]
        )


def test_coding_levels_valid_3_levels_with_ids():
    """Valid 3-level framework with all required ids."""
    levels = ApiCodingFramework(
        root_codes=[
            ApiCodingNode(
                id="water-1",
                name="Water",
                children=[
                    ApiCodingNode(
                        id="dist-1",
                        name="Distribution",
                        children=[ApiCodingNode(id="wait-1", name="Waiting times")],
                    )
                ],
            ),
            ApiCodingNode(
                id="health-1",
                name="Health",
                children=[
                    ApiCodingNode(
                        id="staff-1",
                        name="Staff",
                        children=[ApiCodingNode(id="supplies-1", name="Supplies")],
                    )
                ],
            ),
        ]
    )
    assert len(levels.root_codes) == 2
    assert levels.root_codes[0].id == "water-1"
    assert levels.root_codes[0].children[0].id == "dist-1"
    assert levels.root_codes[0].children[0].children[0].id == "wait-1"


def test_assigned_code_has_all_level_fields():
    """ApiAssignedCode must have all L1/L2/L3 id and name fields."""
    code = ApiAssignedCode(
        coding_level_1_id="type-1",
        coding_level_1_name="Type A",
        coding_level_2_id="cat-1",
        coding_level_2_name="Category A",
        coding_level_3_id="code-1",
        coding_level_3_name="Code A",
        confidence_level_1=0.9,
        confidence_level_2=0.8,
        confidence_level_3=0.7,
        confidence_aggregate=0.7,
        explanation="Test explanation",
    )
    assert code.coding_level_1_id == "type-1"
    assert code.coding_level_1_name == "Type A"
    assert code.coding_level_2_id == "cat-1"
    assert code.coding_level_2_name == "Category A"
    assert code.coding_level_3_id == "code-1"
    assert code.coding_level_3_name == "Code A"


# --- pretty_output header localization ---


@pytest.mark.parametrize(
    "value,expected",
    [
        ("English", "en"),
        ("en", "en"),
        ("French", "fr"),
        ("fr", "fr"),
        ("FRENCH", "fr"),  # case-insensitive
        ("Spanish", "es"),
        ("es", "es"),
        ("Arabic", "ar"),
        ("ar", "ar"),
        ("Russian", "ru"),
        ("ru", "ru"),
        ("Dutch", "nl"),
        ("nl", "nl"),
        ("Ukrainian", "uk"),
        ("uk", "uk"),
    ],
)
def test_resolve_language_maps_supported_names_and_codes(value, expected):
    """Known language names and ISO codes resolve to their code, case-insensitively.

    The static label table is keyed by ISO code, so free-text output_language
    values must normalize to one of the seven supported codes.
    """
    assert _resolve_language(value) == expected


@pytest.mark.parametrize("value", [None, "", "Klingon", "Esperanto", "  "])
def test_resolve_language_falls_back_to_english(value):
    """Absent or unsupported languages fall back to English.

    English is the guaranteed fallback so the block is never left with an
    unresolved placeholder label.
    """
    assert _resolve_language(value) == "en"


def test_resolve_language_logs_warning_on_unsupported_value(caplog):
    """A present-but-unsupported language is logged at WARNING before fallback.

    Operators need visibility into which languages clients request but we do
    not yet localize; the message must echo the requested value.
    """
    with caplog.at_level(logging.WARNING, logger="qfa.api.schemas"):
        assert _resolve_language("Klingon") == "en"
    assert any(
        record.levelno == logging.WARNING and "Klingon" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("value", [None, "", "  ", "French", "en"])
def test_resolve_language_does_not_warn_when_absent_or_supported(value, caplog):
    """Absent or supported languages must not emit a fallback warning.

    The warning is reserved for genuinely unsupported requests, so an omitted
    or recognized language stays quiet.
    """
    with caplog.at_level(logging.WARNING, logger="qfa.api.schemas"):
        _resolve_language(value)
    assert not caplog.records


def test_create_pretty_output_default_language_is_english():
    """language=None reproduces the original English headers (regression guard).

    Existing callers that do not pass a language must see unchanged output.
    """
    out = _create_pretty_output(
        id="doc-1", quality_score=0.85, title="A title", summary="A summary"
    )
    assert "QUALITY:" in out
    assert "TITLE:" in out
    assert "SUMMARY:" in out
    # No translated label leaks in for the default path.
    assert "QUALITÉ" not in out


def test_create_pretty_output_translates_headers_to_requested_language():
    """French headers are translated while IDs, dots, and percentage are unchanged.

    Only QUALITY/TITLE/SUMMARY are localized; technical ID labels and the
    numeric/dot formatting stay as-is.
    """
    out = _create_pretty_output(
        id="doc-1",
        quality_score=0.85,
        title="Un titre",
        summary="Un résumé",
        language="French",
    )
    assert "QUALITÉ" in out
    assert "TITRE" in out
    assert "RÉSUMÉ" in out
    # English headers are replaced, not duplicated.
    assert "QUALITY:" not in out
    assert "TITLE:" not in out
    # Technical label and numeric formatting are untouched.
    assert "Feedback-ID:" in out
    assert "85%" in out


def test_create_pretty_output_aggregate_translates_headers_but_not_ids_label():
    """The aggregate ids= path localizes headers while the IDs label stays as-is.

    The technical ``IDs`` label is not localized even when QUALITY/TITLE/
    SUMMARY are; this exercises the multi-record path used by the bulk endpoint.
    """
    out = _create_pretty_output(
        ids=["doc-1", "doc-2"],
        quality_score=0.85,
        title="Un titre",
        summary="Un résumé",
        language="French",
    )
    assert "QUALITÉ" in out
    assert "QUALITY:" not in out
    # Technical IDs label is not localized and lists both ids.
    assert "IDs:" in out
    assert "doc-1, doc-2" in out


def test_summarize_bulk_response_localizes_pretty_output():
    """ApiSummarizeBulkResponse renders pretty_output in the configured language.

    The computed field must pick up the excluded output_language render input.
    """
    response = ApiSummarizeBulkResponse(
        ids=["doc-1", "doc-2"],
        title="Un titre",
        summary="Un résumé",
        quality_score=0.85,
        output_language="French",
    )
    assert "QUALITÉ" in response.pretty_output


def test_summarize_bulk_response_output_language_excluded_from_serialization():
    """output_language is a render input only and never appears in the JSON body.

    Keeping it out of model_dump avoids polluting the data contract with a
    presentation concern.
    """
    response = ApiSummarizeBulkResponse(
        ids=["doc-1", "doc-2"],
        title="t",
        summary="s",
        quality_score=0.5,
        output_language="French",
    )
    assert "output_language" not in response.model_dump()
