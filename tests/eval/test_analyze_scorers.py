"""Tests for the pure scorers in ``eval/analyze_scorers.py``.

Each test builds a small, hand-made answer key rather than a real one
from ``build_answer_key``, so the score under test stays easy to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langfuse import Evaluation

from analyze_scorers import (
    cited_ids,
    judge_scores,
    paragraphs,
    run_scores,
    score_answer,
)


def _key(
    *,
    family: str = "themes",
    items: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    urgent: list[dict[str, Any]] | None = None,
    decoys: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "family": family,
        "language": "en",
        "items": items or [],
        "rank_checked": None,
        "groups": groups or [],
        "urgent": urgent or [],
        "decoys": decoys or [],
    }


def _item(name: str, keywords: list[str], *, required: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "count": 5,
        "required": required,
        "keywords": keywords,
        "record_ids": [],
    }


def _group(name: str, keywords: list[str], issue_keywords: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "count": 1,
        "keywords": keywords,
        "issue_keywords": issue_keywords,
        "record_ids": [],
    }


def _urgent(record_id: str, keywords: list[str]) -> dict[str, Any]:
    return {"record_id": record_id, "kind": "some_kind", "keywords": keywords}


def _decoy(record_id: str, keywords: list[str]) -> dict[str, Any]:
    return {"record_id": record_id, "keywords": keywords}


def _score(evaluations: list[Evaluation], name: str) -> Evaluation:
    return next(e for e in evaluations if e.name == name)


def test_paragraphs_splits_on_heading_list_continuation_nested_item_and_table() -> None:
    text = (
        "### Heading\n"
        "- Item one\n"
        "  continuation line for item one\n"
        "  - Nested item\n"
        "| Col1 | Col2 |\n"
        "| a | b |"
    )

    assert paragraphs(text) == [
        "### Heading",
        "- Item one\n  continuation line for item one",
        "  - Nested item",
        "| Col1 | Col2 |",
        "| a | b |",
    ]


def test_cited_ids_matches_bold_and_uppercase_but_not_a_longer_number() -> None:
    text = "See **en-0042** and EN-0042, but not en-00421."

    assert cited_ids(text) == {"en-0042"}


def test_judge_scores_null_value_gives_no_score_and_only_single_pass_gets_judge_failed() -> (
    None
):
    full_output = {
        "quality_score": 0.8,
        "faithfulness": 0.9,
        "coverage": 0.7,
        "clarity": 0.6,
    }
    ok = {e.name: e.value for e in judge_scores(full_output, "single_pass")}
    assert ok == {
        "quality_score": 0.8,
        "faithfulness": 0.9,
        "coverage": 0.7,
        "clarity": 0.6,
        "judge_failed": 0,
    }

    null_output = {
        "quality_score": None,
        "faithfulness": None,
        "coverage": None,
        "clarity": None,
    }
    failed = {e.name: e.value for e in judge_scores(null_output, "single_pass")}
    assert failed == {"judge_failed": 1}

    assert judge_scores(null_output, "hierarchical") == []


@pytest.mark.parametrize(
    ("key", "text", "sent_ids", "name", "value"),
    [
        pytest.param(
            _key(items=[_item("food", ["food shortage", "hunger"])]),
            "There is a severe food shortage and widespread hunger in the camp.",
            [],
            "item_recall",
            1.0,
            id="item_recall",
        ),
        pytest.param(
            _key(groups=[_group("children", ["children"], ["no school access"])]),
            "Many children are affected.",
            [],
            "group_coverage",
            1.0,
            id="group_coverage",
        ),
        pytest.param(
            _key(groups=[_group("children", ["children"], ["no school access"])]),
            "Many children have no school access this term.",
            [],
            "group_issue_pairs",
            1.0,
            id="group_issue_pairs",
        ),
        pytest.param(
            _key(),
            "See en-0001 and en-0099.",
            ["en-0001", "en-0002"],
            "ids_cited",
            1,
            id="ids_cited",
        ),
        pytest.param(
            _key(),
            "See en-0001 and en-0099.",
            ["en-0001"],
            "unknown_ids_cited",
            1,
            id="unknown_ids_cited",
        ),
        pytest.param(
            _key(
                family="protection",
                urgent=[_urgent("en-0042", ["sex", "exchange", "aid"])],
            ),
            "The record en-0042 raises a concern.",
            [],
            "urgent_ids_cited",
            1.0,
            id="urgent_ids_cited",
        ),
        pytest.param(
            _key(
                family="protection",
                urgent=[_urgent("en-0042", ["sex", "exchange", "aid"])],
            ),
            "The record describes sex in exchange for aid, which raises concern.",
            [],
            "urgent_described",
            1.0,
            id="urgent_described",
        ),
        pytest.param(
            _key(
                family="protection",
                decoys=[_decoy("en-0099", ["argument", "conflict"])],
            ),
            "The record en-0099 is not urgent.",
            [],
            "decoys_cited",
            1.0,
            id="decoys_cited",
        ),
    ],
)
def test_score_answer_computes_each_reference_score(
    key: dict[str, Any],
    text: str,
    sent_ids: list[str],
    name: str,
    value: float,
) -> None:
    evaluations = score_answer(text, key, sent_ids)

    assert _score(evaluations, name).value == pytest.approx(value)


def test_score_answer_omits_urgent_and_decoy_scores_outside_protection() -> None:
    key = _key(
        family="themes",
        urgent=[_urgent("en-0042", ["sex", "exchange", "aid"])],
        decoys=[_decoy("en-0099", ["argument", "conflict"])],
    )

    names = {
        e.name for e in score_answer("en-0042 and en-0099 are mentioned.", key, [])
    }

    assert names.isdisjoint({"urgent_ids_cited", "urgent_described", "decoys_cited"})


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        pytest.param(
            "One minor theme is transport.",
            _key(
                groups=[
                    _group(
                        "unaccompanied_minors",
                        ["unaccompanied", "separated"],
                        ["missing", "risk", "alone"],
                    )
                ]
            ),
            {"group_coverage": 0.0},
            id="minor-theme-does-not-credit-unaccompanied-minors",
        ),
        pytest.param(
            "So far, nobody has answered.",
            _key(groups=[_group("access", ["too far"], ["queue", "distance", "walk"])]),
            {"group_coverage": 0.0},
            id="so-far-does-not-match-too-far",
        ),
        pytest.param(
            "women and children",
            _key(
                groups=[
                    _group("women", ["women"], ["maternal care"]),
                    _group("children", ["children"], ["no school access"]),
                ]
            ),
            {"group_coverage": 1.0, "group_issue_pairs": 0.0},
            id="women-and-children-covers-groups-not-issue-pairs",
        ),
    ],
)
def test_false_positive_strings_get_no_undeserved_credit(
    text: str, key: dict[str, Any], expected: dict[str, float]
) -> None:
    evaluations = score_answer(text, key, sent_ids=[])

    for name, value in expected.items():
        assert _score(evaluations, name).value == pytest.approx(value)


@dataclass
class _FakeResult:
    output: dict[str, Any]
    evaluations: list[Evaluation] = field(default_factory=list)


def test_run_scores_excludes_null_scores_and_counts_outcomes_and_n_scored() -> None:
    results = [
        _FakeResult(
            output={"outcome": "ok"},
            evaluations=[
                Evaluation(name="quality_score", value=0.8),
                Evaluation(name="judge_failed", value=0),
            ],
        ),
        _FakeResult(
            # judge_scores() leaves out a null quality_score rather than
            # recording it as value=None, so a "null score" is simply
            # absent here too.
            output={"outcome": "ok"},
            evaluations=[Evaluation(name="judge_failed", value=1)],
        ),
        _FakeResult(output={"outcome": "payload_too_large"}),
    ]

    by_name = {s.name: s for s in run_scores(results)}

    assert by_name["mean_quality_score"].value == pytest.approx(0.8)
    assert by_name["mean_quality_score"].metadata == {"n_scored": 1, "n_items": 3}
    assert by_name["judge_failure_rate"].value == pytest.approx(0.5)
    assert by_name["judge_failure_rate"].metadata == {"n_scored": 2, "n_items": 3}
    assert by_name["outcome_count::ok"].value == 2
    assert by_name["outcome_count::payload_too_large"].value == 1


def test_run_scores_group_coverage_reads_each_items_group_coverage_metadata() -> None:
    results = [
        _FakeResult(
            output={"outcome": "ok"},
            evaluations=[
                Evaluation(
                    name="group_coverage",
                    value=1.0,
                    metadata={"covered": ["children", "women"], "missed": []},
                )
            ],
        ),
        _FakeResult(
            output={"outcome": "ok"},
            evaluations=[
                Evaluation(
                    name="group_coverage",
                    value=0.5,
                    metadata={"covered": ["children"], "missed": ["women"]},
                )
            ],
        ),
    ]

    by_name = {s.name: s for s in run_scores(results)}

    assert by_name["group_coverage::children"].value == pytest.approx(1.0)
    assert by_name["group_coverage::children"].metadata == {"n_items": 2}
    assert by_name["group_coverage::women"].value == pytest.approx(0.5)
    assert by_name["group_coverage::women"].metadata == {"n_items": 2}
