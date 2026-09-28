r"""Build and upload the analyze-eval case set, one case per English prompt.

A case pairs one prompt with the pool's records in send order and an
answer key: what a correct response to that prompt should contain,
computed straight from the labels already on the pool records and the
shared vocabulary, so nobody edits a key by hand and it can never drift
from the pool it scores.

``build_answer_key`` and the scorers that read its output take a record
as ``{id, content, metadata}`` and a vocabulary entry as ``{kind, name,
keywords: {<language>: [...]}, issue_keywords: {...}}`` — the shapes
``upload_pool.py`` uploads. ``record_from_item``/``vocab_from_item``
rebuild that shape from what Langfuse hands back.

Run::

    uv run python eval/make_analyze_cases.py --dry-run
    uv run python eval/make_analyze_cases.py

Prerequisites
-------------
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` — the
  Langfuse project to read the pool from and upload the case set to.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from langfuse import get_client

from _common import load_env, upload_items
from pool_spec import (
    FAMILY_LABEL,
    FREQUENT_CASES_DATASET,
    PROMPTS_DATASET,
    RECORDS_DATASET,
    REQUIRED_MIN_RECORDS,
    VOCAB_DATASET,
)

# How much bigger the running count must be than the next item's count
# for their frequency order to be worth checking.
RANK_RATIO = 1.5

# The language instruction the service adds for each case language. Always
# set, because an unset output_language adds no language instruction.
OUTPUT_LANGUAGE = {"en": "English"}


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


def record_from_item(item: Any) -> dict[str, Any]:
    """The pool record for a ``feedback/records-en-v1`` dataset item, in YAML shape.

    The inverse of ``upload_pool.build_items``: ``created`` moves back
    from ``item.input["metadata"]`` into the record's own ``metadata``,
    next to the record's other labels in ``item.metadata``.
    """
    return {
        "id": item.input["id"],
        "content": item.input["content"],
        "metadata": {**item.metadata, "created": item.input["metadata"]["created"]},
    }


def vocab_from_item(item: Any) -> dict[str, Any]:
    """The vocabulary entry for a ``feedback/vocab-v1`` dataset item, in YAML shape.

    The inverse of ``upload_pool.build_vocab_items``.
    """
    entry: dict[str, Any] = {
        "kind": item.metadata["kind"],
        "name": item.metadata["name"],
        "keywords": item.input["keywords"],
    }
    if "issue_keywords" in item.input:
        entry["issue_keywords"] = item.input["issue_keywords"]
    return entry


def send_order(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """``records``, newest ``created`` first, then by id — the EspoCRM send order.

    ``created`` timestamps are ``%Y-%m-%dT%H:%M:%SZ`` strings, which sort
    chronologically as plain text, so no parsing is needed.
    """
    by_id = sorted(records, key=lambda record: record["id"])
    return sorted(by_id, key=lambda record: record["metadata"]["created"], reverse=True)


def key_positions(
    key: Mapping[str, Any], record_ids: Sequence[str]
) -> dict[str, list[int]]:
    """Where each of ``key``'s groups and urgent records sit in ``record_ids``.

    Lets a person confirm that groups and urgent records are spread
    across the send order rather than clustered together. Items and
    decoys are not positioned: items are large by definition, and only
    the group and urgent positions matter for a hidden position effect.
    """
    position = {record_id: i for i, record_id in enumerate(record_ids)}
    positions: dict[str, list[int]] = {}
    for group in key["groups"]:
        positions[f"groups:{group['name']}"] = sorted(
            position[record_id] for record_id in group["record_ids"]
        )
    for urgent in key["urgent"]:
        positions[f"urgent:{urgent['record_id']}"] = [position[urgent["record_id"]]]
    return positions


def build_cases(
    records: Sequence[Mapping[str, Any]],
    vocab: Sequence[Mapping[str, Any]],
    prompts: Sequence[Any],
    read_at: datetime,
) -> list[dict[str, Any]]:
    """One case per English prompt, sending every one of ``records``.

    ``records`` and ``vocab`` must be as they stood at ``read_at``, so
    the send order and the answer key describe the same records; the
    caller fetches both at that version. Stops the whole build on the
    first prompt that cannot become a case, rather than uploading a
    partial case set.

    Raises
    ------
    SystemExit
        A prompt is tagged ``unplanted``, or its family (other than
        ``protection``) has no required item in its answer key.
    """
    record_ids = [record["id"] for record in send_order(records)]
    version = read_at.isoformat()

    cases = []
    for prompt in prompts:
        if prompt.metadata["language"] != "en":
            continue

        prompt_id = prompt.metadata["prompt_id"]
        family = prompt.metadata["family"]
        language = prompt.metadata["language"]

        if family == "unplanted":
            raise SystemExit(f"unplanted prompt: {prompt_id}")

        key = build_answer_key(records, vocab, family, language)
        if family != "protection" and not any(
            item["required"] for item in key["items"]
        ):
            raise SystemExit(f"no required item for {family}")

        cases.append(
            {
                "id": f"{prompt_id}-{language}",
                "input": {
                    "prompt_id": prompt_id,
                    "prompt": prompt.input,
                    "mode": "single_pass",
                    "output_language": OUTPUT_LANGUAGE[language],
                    "record_ids": record_ids,
                },
                "expected_output": key,
                "metadata": {
                    "records_dataset": RECORDS_DATASET,
                    "records_version": version,
                    "vocab_dataset": VOCAB_DATASET,
                    "vocab_version": version,
                    "prompt_dataset": PROMPTS_DATASET,
                    "prompt_id": prompt_id,
                    "family": family,
                    "language": language,
                    "key_positions": key_positions(key, record_ids),
                },
            }
        )
    return cases


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and upload the analyze frequent case set.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check, but write nothing to Langfuse.",
    )
    return parser.parse_args()


def main() -> None:
    """Build the frequent case set from the live pool, vocabulary and prompts, and upload it."""
    args = _parse_args()
    load_env()
    read_at = datetime.now(UTC)

    langfuse = get_client()
    records = [
        record_from_item(item)
        for item in langfuse.get_dataset(RECORDS_DATASET, version=read_at).items
    ]
    vocab = [
        vocab_from_item(item)
        for item in langfuse.get_dataset(VOCAB_DATASET, version=read_at).items
    ]
    prompts = langfuse.get_dataset(PROMPTS_DATASET, version=read_at).items

    cases = build_cases(records, vocab, prompts, read_at)

    for case in cases:
        n_records = len(case["input"]["record_ids"])
        print(f"{case['id']}: {n_records} records, family={case['metadata']['family']}")
        print(f"  positions: {case['metadata']['key_positions']}")

    report = upload_items(langfuse, FREQUENT_CASES_DATASET, cases, dry_run=args.dry_run)
    print(f"\nDataset: {FREQUENT_CASES_DATASET}{' (dry run)' if args.dry_run else ''}")
    print(
        f"{len(report.created)} created, {len(report.unchanged)} unchanged, {len(report.updated)} updated"
    )


if __name__ == "__main__":
    main()
