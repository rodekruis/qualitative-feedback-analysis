"""Tests for the pure scorers in ``eval/analyze_scorers.py``.

Each test builds a small, hand-made answer key rather than a real one
from ``build_answer_key``, so the score under test stays easy to read.
An answer cites a record the way a real one does, by writing its id.
"""

from __future__ import annotations

from typing import Any

import pytest
from langfuse import Evaluation

from analyze_scorers import cited_ids, judge_scores, score_answer, sections


def _key(
    *,
    family: str = "needs",
    labels: dict[str, Any] | None = None,
    required: list[str] | None = None,
    groups: dict[str, list[str]] | None = None,
    urgent: list[str] | None = None,
) -> dict[str, Any]:
    key: dict[str, Any] = {"family": family, "groups": groups or {}}
    if urgent is not None:
        key["urgent"] = urgent
    else:
        key["labels"] = labels or {}
        key["required"] = required or []
    return key


def _score(evaluations: list[Evaluation], name: str) -> Evaluation:
    return next(e for e in evaluations if e.name == name)


def _names(evaluations: list[Evaluation]) -> set[str]:
    return {e.name for e in evaluations}


def test_sections_start_at_a_heading_or_an_all_bold_line_but_not_a_bold_label() -> None:
    text = (
        "Overview of the answer.\n"
        "## Food\n"
        "Records en-0001 and en-0002.\n"
        "**1. Shelter (5 records)**\n"
        "Record en-0003.\n"
        "**Note:** this line labels the text after it.\n"
        "Record en-0004."
    )

    assert sections(text) == [
        "Overview of the answer.",
        "## Food\nRecords en-0001 and en-0002.",
        "**1. Shelter (5 records)**\nRecord en-0003.\n"
        "**Note:** this line labels the text after it.\nRecord en-0004.",
    ]


def test_a_top_level_list_item_starts_a_section_but_an_indented_one_does_not() -> None:
    """Answers with no headings report one point per list item."""
    text = (
        "- Rumours about eligibility are circulating.\n"
        "  IDs: en-0001, en-0002\n"
        "1. **Food distribution problems**\n"
        "   - rations too small\n"
        "   Records: en-0003"
    )

    assert sections(text) == [
        "- Rumours about eligibility are circulating.\n  IDs: en-0001, en-0002",
        "1. **Food distribution problems**\n   - rations too small\n   Records: en-0003",
    ]


def test_cited_ids_matches_bold_and_uppercase_but_not_a_longer_number() -> None:
    text = "See **en-0042** and EN-0042, but not en-00421."

    assert cited_ids(text) == {"en-0042"}


def test_judge_scores_leave_out_a_null_value() -> None:
    full_output = {
        "quality_score": 0.8,
        "faithfulness": 0.9,
        "coverage": 0.7,
        "clarity": 0.6,
    }

    assert {e.name: e.value for e in judge_scores(full_output)} == full_output
    assert judge_scores(dict.fromkeys(full_output)) == []


def test_item_recall_counts_an_item_found_when_its_sections_hold_half_its_records() -> (
    None
):
    key = _key(
        labels={
            "en-0001": "food",
            "en-0002": "food",
            "en-0003": "food",
            "en-0004": "food",
            "en-0005": "shelter",
            "en-0006": "shelter",
        },
        required=["food", "shelter"],
    )
    # The food section holds half of the food records; shelter is absent.
    text = "## Food\nRecords en-0001 and en-0002 report a shortage."

    score = _score(score_answer(text, key, list(key["labels"])), "item_recall")

    assert score.value == pytest.approx(0.5)
    assert score.comment == "1/2, food 2/4, shelter 0/2"
    assert score.metadata == {"found": ["food"], "missed": ["shelter"]}


def test_a_section_whose_records_tie_on_two_labels_gets_no_label() -> None:
    key = _key(
        labels={"en-0001": "food", "en-0002": "shelter"},
        required=["food", "shelter"],
    )
    text = "## Both\nRecords en-0001 and en-0002."

    evaluations = score_answer(text, key, list(key["labels"]))

    assert _score(evaluations, "item_recall").value == pytest.approx(0.0)
    # No section has a required label, so precision is not scored at all.
    assert "citation_precision" not in _names(evaluations)


def test_citation_precision_counts_the_ids_that_do_not_carry_the_sections_label() -> (
    None
):
    key = _key(
        labels={
            "en-0001": "food",
            "en-0002": "food",
            "en-0003": None,
            "en-0004": "shelter",
        },
        required=["food"],
    )
    text = "## Food\nRecords en-0001, en-0002 and en-0003 report a shortage."

    score = _score(score_answer(text, key, list(key["labels"])), "citation_precision")

    assert score.value == pytest.approx(2 / 3)
    assert score.comment == "2/3, other: en-0003"
    assert score.metadata == {"other": ["en-0003"]}


def test_an_id_the_harness_did_not_send_is_ignored_by_the_section_scores() -> None:
    key = _key(labels={"en-0001": "food", "en-0002": "food"}, required=["food"])
    text = "## Food\nRecords en-0001, en-0002 and en-0900."

    evaluations = score_answer(text, key, ["en-0001", "en-0002"])

    # en-0900 changes neither the section's label nor its precision.
    assert _score(evaluations, "citation_precision").comment == "2/2"
    assert _score(evaluations, "ids_cited").value == 2
    assert _score(evaluations, "unknown_ids_cited").value == 1


def test_group_recall_counts_the_groups_records_cited_anywhere() -> None:
    key = _key(
        labels={"en-0001": "food", "en-0002": "food", "en-0003": "food"},
        required=["food"],
        groups={"children": ["en-0001", "en-0002"], "women": ["en-0003"]},
    )
    text = "Record en-0001 only."

    evaluations = score_answer(text, key, list(key["labels"]))

    children = _score(evaluations, "group_recall::children")
    assert children.value == pytest.approx(0.5)
    assert children.comment == "1/2, missed: en-0002"
    assert _score(evaluations, "group_recall::women").value == pytest.approx(0.0)


def test_only_the_protection_key_gets_urgent_ids_cited() -> None:
    protection = _key(family="protection", urgent=["en-0001", "en-0002"])
    needs = _key(labels={"en-0001": "food"}, required=[])
    text = "Record en-0001 needs follow-up."

    score = _score(score_answer(text, protection, ["en-0001"]), "urgent_ids_cited")

    assert score.value == pytest.approx(0.5)
    assert score.comment == "1/2, missed: en-0002"
    assert "urgent_ids_cited" not in _names(score_answer(text, needs, ["en-0001"]))
