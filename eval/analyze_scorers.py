"""Pure scorers that compare an analyze answer with its answer key.

Every keyword match is a whole word or phrase, via ``_common.contains_term``.
These functions make no network call: ``score_answer`` and ``judge_scores``
work on one saved answer, and ``run_scores`` works on the evaluations they
already attached to a batch of results. All three plug into a Langfuse
experiment as item and run evaluators unchanged.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langfuse import Evaluation

from _common import contains_term, normalize

# How many of an item's keywords must appear in the whole answer for the
# item to count as covered.
ITEM_HIT_THRESHOLD = 2

# How many of an urgent record's own keywords must appear in one paragraph
# for the record to count as described.
URGENT_KEYWORD_THRESHOLD = 2

JUDGE_SCORE_NAMES = ("quality_score", "faithfulness", "coverage", "clarity")

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_TABLE_ROW = re.compile(r"^\s*\|")
_CITED_ID = re.compile(r"\b[a-z]{2}-\d{4}\b")


def paragraphs(text: str) -> list[str]:
    """``text`` split into paragraphs.

    A paragraph starts at a blank line, a heading, a list item at any
    depth, or a table row; any other line joins the paragraph above it.
    A multi-row table yields one paragraph per row, since each row starts
    a new one.
    """
    result: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                result.append("\n".join(current))
                current = []
            continue
        starts_new = bool(
            _HEADING.match(line) or _LIST_ITEM.match(line) or _TABLE_ROW.match(line)
        )
        if starts_new and current:
            result.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        result.append("\n".join(current))
    return result


def cited_ids(text: str) -> set[str]:
    """The record ids ``text`` cites, matched case-insensitively."""
    return set(_CITED_ID.findall(normalize(text)))


def judge_scores(output: Mapping[str, Any], mode: str) -> list[Evaluation]:
    """The service's own judge scores, plus ``judge_failed`` for ``single_pass``.

    A score missing or explicitly null in ``output`` is left out, never
    turned into a 0. ``judge_failed`` is 1 when ``quality_score`` is null,
    else 0. Hierarchical mode reports its own confidence instead, so it
    gets no ``judge_failed``.
    """
    evaluations = [
        Evaluation(name=name, value=output[name])
        for name in JUDGE_SCORE_NAMES
        if output.get(name) is not None
    ]
    if mode == "single_pass":
        failed = 0 if output.get("quality_score") is not None else 1
        evaluations.append(Evaluation(name="judge_failed", value=failed))
    return evaluations


def _hits(text: str, keywords: Sequence[str]) -> int:
    """How many of ``keywords`` appear in ``text`` as a whole word or phrase."""
    return sum(1 for keyword in keywords if contains_term(text, keyword))


def _max_hits_in_one_paragraph(paras: Sequence[str], keywords: Sequence[str]) -> int:
    return max((_hits(paragraph, keywords) for paragraph in paras), default=0)


def _keyword_and_issue_in_one_paragraph(
    paras: Sequence[str], keywords: Sequence[str], issue_keywords: Sequence[str]
) -> bool:
    return any(
        _hits(paragraph, keywords) and _hits(paragraph, issue_keywords)
        for paragraph in paras
    )


def _share(
    name: str,
    entries: Sequence[Mapping[str, Any]],
    identifier: str,
    covered: Callable[[Mapping[str, Any]], bool],
) -> Evaluation | None:
    """The share of ``entries`` for which ``covered`` holds; ``None`` if ``entries`` is empty.

    An empty key part is "not scored", so it yields no ``Evaluation`` at
    all rather than a share of 0.
    """
    if not entries:
        return None
    covered_ids = [entry[identifier] for entry in entries if covered(entry)]
    missed_ids = [
        entry[identifier] for entry in entries if entry[identifier] not in covered_ids
    ]
    comment = f"{len(covered_ids)}/{len(entries)}"
    if missed_ids:
        comment += f", missed: {', '.join(missed_ids)}"
    return Evaluation(
        name=name,
        value=len(covered_ids) / len(entries),
        comment=comment,
        metadata={"covered": covered_ids, "missed": missed_ids},
    )


def score_answer(
    text: str, key: Mapping[str, Any], sent_ids: Sequence[str]
) -> list[Evaluation]:
    """The reference scores for ``text`` against its answer ``key``.

    The urgent and decoy scores are computed only when ``key["family"]``
    is ``"protection"``; every other score comes from ``key["items"]``
    and ``key["groups"]``, whatever the family.
    """
    paras = paragraphs(text)
    cited = cited_ids(text)
    sent = set(sent_ids)

    scores = [
        _share(
            "item_recall",
            [item for item in key["items"] if item["required"]],
            "name",
            lambda item: _hits(text, item["keywords"]) >= ITEM_HIT_THRESHOLD,
        ),
        _share(
            "group_coverage",
            key["groups"],
            "name",
            lambda group: _hits(text, group["keywords"]) > 0,
        ),
        _share(
            "group_issue_pairs",
            key["groups"],
            "name",
            lambda group: _keyword_and_issue_in_one_paragraph(
                paras, group["keywords"], group["issue_keywords"]
            ),
        ),
    ]

    if key["family"] == "protection":
        scores += [
            _share(
                "urgent_ids_cited",
                key["urgent"],
                "record_id",
                lambda urgent: urgent["record_id"] in cited,
            ),
            _share(
                "urgent_described",
                key["urgent"],
                "record_id",
                lambda urgent: (
                    _max_hits_in_one_paragraph(paras, urgent["keywords"])
                    >= URGENT_KEYWORD_THRESHOLD
                ),
            ),
            _share(
                "decoys_cited",
                key["decoys"],
                "record_id",
                lambda decoy: decoy["record_id"] in cited,
            ),
        ]

    evaluations = [score for score in scores if score is not None]
    evaluations.append(Evaluation(name="ids_cited", value=len(cited & sent)))
    evaluations.append(Evaluation(name="unknown_ids_cited", value=len(cited - sent)))
    return evaluations


def run_scores(item_results: Sequence[Any]) -> list[Evaluation]:
    """Run-level scores computed purely from each item's evaluations and output.

    Reuses the item scores in ``result.evaluations`` and the outcome in
    ``result.output`` rather than re-deriving anything from the raw
    answers. A numeric score gets ``mean_<name>``, averaged only over the
    items that have it and never counting a missing one as 0
    (``judge_failed`` becomes ``judge_failure_rate`` instead of
    ``mean_judge_failed``). ``group_coverage::<group>`` reads every
    item's ``group_coverage`` metadata to report coverage of one specific
    group, over only the items whose key held it.
    """
    numeric_values: dict[str, list[float]] = {}
    group_totals: Counter[str] = Counter()
    group_covered: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()

    for result in item_results:
        output = result.output
        outcome = output.get("outcome") if isinstance(output, Mapping) else None
        if outcome is not None:
            outcome_counts[outcome] += 1

        for evaluation in result.evaluations:
            value = evaluation.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            numeric_values.setdefault(evaluation.name, []).append(value)

            if evaluation.name == "group_coverage" and evaluation.metadata:
                for group in evaluation.metadata.get("covered") or []:
                    group_totals[group] += 1
                    group_covered[group] += 1
                for group in evaluation.metadata.get("missed") or []:
                    group_totals[group] += 1

    scores: list[Evaluation] = []
    for name, values in numeric_values.items():
        score_name = "judge_failure_rate" if name == "judge_failed" else f"mean_{name}"
        scores.append(
            Evaluation(
                name=score_name,
                value=sum(values) / len(values),
                metadata={"n_scored": len(values), "n_items": len(item_results)},
            )
        )

    for group, total in group_totals.items():
        scores.append(
            Evaluation(
                name=f"group_coverage::{group}",
                value=group_covered[group] / total,
                metadata={"n_items": total},
            )
        )

    for outcome, count in outcome_counts.items():
        scores.append(Evaluation(name=f"outcome_count::{outcome}", value=count))

    return scores
