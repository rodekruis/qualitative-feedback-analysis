r"""Compute analyze-eval answer keys from pool records and vocabulary.

An answer key says what a correct response to one analyze prompt should
contain. It is computed straight from the labels already on the pool
records and the shared vocabulary, so nobody edits a key by hand and it
can never drift from the pool it scores.

Run the module's checks manually against the real pool; the shapes it
reads are those of ``upload_pool.py``: a record is ``{id, content,
metadata}`` with the labels in ``metadata``, and a vocabulary entry is
``{kind, name, keywords: {<language>: [...]}, issue_keywords: {...}}``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pool_spec import FAMILY_LABEL, REQUIRED_MIN_RECORDS

# How much bigger the running count must be than the next item's count
# for their frequency order to be worth checking.
RANK_RATIO = 1.5


def build_answer_key(
    records: Sequence[Mapping[str, Any]],
    vocab: Sequence[Mapping[str, Any]],
    family: str,
    language: str,
    *,
    required_min: int = REQUIRED_MIN_RECORDS,
) -> dict[str, Any]:
    """The answer key for ``family``, computed from ``records`` and ``vocab``.

    ``items`` is the family's content: one entry per label value, with
    the records that set it and the vocabulary's keywords for it.
    ``protection`` has no items, because ``urgent`` and ``decoys`` score
    it instead. ``rank_checked`` is set only for ``themes`` and
    ``needs``, and only when the frequency order of two or more required
    items is safe to compare. ``groups`` holds only the groups with a
    record that also sets one of the family's labels — for ``themes``,
    every record qualifies, so every group is included. ``urgent`` and
    ``decoys`` are always computed, from every record with an
    ``urgent_kind`` or a ``decoy`` flag, whatever ``family`` is.

    An empty ``items``, ``rank_checked``, or ``groups`` means "not
    scored", never "wrong".

    Raises
    ------
    SystemExit
        A label value on a record, or a group it lists, has no matching
        entry in ``vocab``.
    """
    vocab_index = {(entry["kind"], entry["name"]): entry for entry in vocab}

    def keywords(kind: str, name: str, field: str = "keywords") -> list[str]:
        entry = vocab_index.get((kind, name))
        if entry is None:
            raise SystemExit(f"no vocabulary entry for {name}")
        return list(entry.get(field, {}).get(language) or [])

    labels = FAMILY_LABEL[family]

    items: list[dict[str, Any]] = []
    if family != "protection":
        (label,) = labels
        ids_by_value: dict[str, list[str]] = {}
        for record in records:
            value = record["metadata"].get(label)
            if value:
                ids_by_value.setdefault(value, []).append(record["id"])
        items = _sorted_by_count(
            {
                "name": name,
                "count": len(ids),
                "required": len(ids) >= required_min,
                "keywords": keywords(label, name),
                "record_ids": sorted(ids),
            }
            for name, ids in ids_by_value.items()
        )

    rank_checked = _rank_checked(items) if family in ("themes", "needs") else None

    ids_by_group: dict[str, list[str]] = {}
    for record in records:
        if not any(record["metadata"].get(label) for label in labels):
            continue
        for group in record["metadata"].get("groups") or []:
            ids_by_group.setdefault(group, []).append(record["id"])
    groups = _sorted_by_count(
        {
            "name": name,
            "count": len(ids),
            "keywords": keywords("group", name),
            "issue_keywords": keywords("group", name, field="issue_keywords"),
            "record_ids": sorted(ids),
        }
        for name, ids in ids_by_group.items()
    )

    urgent = sorted(
        (
            {
                "record_id": record["id"],
                "kind": record["metadata"]["urgent_kind"],
                "keywords": list(record["metadata"].get("keywords") or []),
            }
            for record in records
            if record["metadata"].get("urgent_kind")
        ),
        key=lambda entry: entry["record_id"],
    )
    decoys = sorted(
        (
            {
                "record_id": record["id"],
                "keywords": list(record["metadata"].get("keywords") or []),
            }
            for record in records
            if record["metadata"].get("decoy")
        ),
        key=lambda entry: entry["record_id"],
    )

    return {
        "family": family,
        "language": language,
        "items": items,
        "rank_checked": rank_checked,
        "groups": groups,
        "urgent": urgent,
        "decoys": decoys,
    }


def _sorted_by_count(entries: Any) -> list[dict[str, Any]]:
    """``entries``, highest ``count`` first, then by ``name``."""
    return sorted(entries, key=lambda entry: (-entry["count"], entry["name"]))


def _rank_checked(items: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """The required items, highest count first, whose order is safe to compare.

    Starts from the highest count and keeps adding the next required
    item while the running count is at least ``RANK_RATIO`` times the
    next one. Stops there, because once neighbours are that close a
    small labeling difference could flip their order. A run of one item
    has no order to check.
    """
    required = [item for item in items if item["required"]]
    if not required:
        return None
    chosen = [required[0]]
    last_count = required[0]["count"]
    for item in required[1:]:
        if last_count < RANK_RATIO * item["count"]:
            break
        chosen.append(item)
        last_count = item["count"]
    return [item["name"] for item in chosen] if len(chosen) >= 2 else None
