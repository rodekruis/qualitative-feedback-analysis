"""Pure scorers that compare an analyze answer with its answer key.

The reference scores read the record ids the answer cites, never its
wording: a record id has no synonyms, so an answer that reports an item
in unexpected words still gets credit for it. An id counts only if the
harness sent it.

These functions make no network call, so a saved answer can be scored
again at no cost. They plug into a Langfuse experiment as item
evaluators unchanged.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from langfuse import Evaluation

# The share of an item's records that the sections carrying its label
# must hold for the answer to count as reporting that item.
FOUND_SHARE = 0.5

JUDGE_SCORE_NAMES = ("quality_score", "faithfulness", "coverage", "clarity")

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ALL_BOLD = re.compile(r"^\*\*(?:(?!\*\*).)+\*\*$")
# No leading space: an indented item is a detail of the point above it,
# and a nested item under "1. " is already indented three spaces.
_TOP_LEVEL_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_CITED_ID = re.compile(r"\b[a-z]{2}-\d{4}\b")


def sections(text: str) -> list[str]:
    """``text`` split into sections, one per point the answer reports.

    A section starts at a markdown heading, at a line that is all bold
    such as ``**1. Food security (5 records)**``, or at a top-level list
    item — answers report one point per heading or per list item, and
    which of the two they use varies by prompt. An indented list item
    stays inside its point, so the detail bullets under a heading do not
    split it. A bold label with text after it, such as ``**Food:** the
    camp ran out``, also stays inside the section above it. Anything
    before the first section start is its own section.
    """
    result: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        starts_new = bool(
            _HEADING.match(line)
            or _ALL_BOLD.match(line.strip())
            or _TOP_LEVEL_LIST_ITEM.match(line)
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
    return set(_CITED_ID.findall(text.lower()))


def judge_scores(output: Mapping[str, Any]) -> list[Evaluation]:
    """The service's own judge scores.

    A score missing or explicitly null in ``output`` is left out, never
    turned into a 0, so a failed judge shows as a missing score.
    """
    return [
        Evaluation(name=name, value=output[name])
        for name in JUDGE_SCORE_NAMES
        if output.get(name) is not None
    ]


def _section_label(ids: Sequence[str], labels: Mapping[str, Any]) -> Any:
    """The label most of ``ids`` carry, or ``None`` if two labels tie.

    Unlabelled records count as a label of their own, so a section whose
    records mostly carry no label reports something the family does not
    cover and counts for no item.
    """
    ranked = Counter(labels.get(record_id) for record_id in ids).most_common(2)
    if not ranked:
        return None
    if len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _cited_share(name: str, record_ids: Sequence[str], cited: set[str]) -> Evaluation:
    """The share of ``record_ids`` that the answer cites anywhere."""
    found = [record_id for record_id in record_ids if record_id in cited]
    missed = [record_id for record_id in record_ids if record_id not in cited]
    comment = f"{len(found)}/{len(record_ids)}"
    if missed:
        comment += f", missed: {', '.join(missed)}"
    return Evaluation(
        name=name,
        value=len(found) / len(record_ids),
        comment=comment,
        metadata={"cited": found, "missed": missed},
    )


def score_answer(
    text: str, key: Mapping[str, Any], sent_ids: Sequence[str]
) -> list[Evaluation]:
    """The reference scores for ``text`` against its answer ``key``.

    Splits ``text`` into sections, gives each section the label that most
    of its cited ids carry, and reads the items from that. A key part
    that is missing or empty yields no score at all, never a 0: the
    protection key has no ``labels``, so it gets no item scores, and only
    it has ``urgent``.
    """
    sent = set(sent_ids)
    cited = cited_ids(text)
    labels: Mapping[str, Any] = key.get("labels") or {}
    required: Sequence[str] = key.get("required") or []

    # Each section's own ids, deduplicated, and the label they give it.
    per_section = [sorted(cited_ids(section) & sent) for section in sections(text)]
    labelled = [(ids, _section_label(ids, labels)) for ids in per_section if ids]

    scores: list[Evaluation] = []

    if required:
        found_by_label: dict[str, set[str]] = {}
        for ids, label in labelled:
            if label is not None:
                found_by_label.setdefault(label, set()).update(ids)
        found, missed, counts = [], [], []
        for value in required:
            records = {
                record_id for record_id, label in labels.items() if label == value
            }
            hits = records & found_by_label.get(value, set())
            counts.append(f"{value} {len(hits)}/{len(records)}")
            (found if len(hits) >= FOUND_SHARE * len(records) else missed).append(value)
        scores.append(
            Evaluation(
                name="item_recall",
                value=len(found) / len(required),
                comment=f"{len(found)}/{len(required)}, {', '.join(counts)}",
                metadata={"found": found, "missed": missed},
            )
        )

    # An id in two sections with a required label is counted in both.
    placed = [
        (record_id, label)
        for ids, label in labelled
        if label in required
        for record_id in ids
    ]
    if placed:
        other = sorted({r for r, label in placed if labels.get(r) != label})
        matching = sum(1 for r, label in placed if labels.get(r) == label)
        comment = f"{matching}/{len(placed)}"
        if other:
            comment += f", other: {', '.join(other)}"
        scores.append(
            Evaluation(
                name="citation_precision",
                value=matching / len(placed),
                comment=comment,
                metadata={"other": other},
            )
        )

    for name, record_ids in (key.get("groups") or {}).items():
        if record_ids:
            scores.append(_cited_share(f"group_recall::{name}", record_ids, cited))

    if key["family"] == "protection" and key.get("urgent"):
        scores.append(_cited_share("urgent_ids_cited", key["urgent"], cited))

    scores.append(Evaluation(name="ids_cited", value=len(cited & sent)))
    scores.append(Evaluation(name="unknown_ids_cited", value=len(cited - sent)))
    return scores
