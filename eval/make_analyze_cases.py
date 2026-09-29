r"""Build and upload the analyze-eval case set, one case per English prompt.

A case pairs one prompt with the pool's records in send order and an
answer key: what a correct response to that prompt should contain,
computed straight from the labels already on the pool records, so nobody
edits a key by hand and it can never drift from the pool it scores.

``build_answer_key`` and the scorers that read its output take a record
as ``{id, content, metadata}`` — the shape ``upload_pool.py`` uploads.
``record_from_item`` rebuilds that shape from what Langfuse hands back.

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
from collections import Counter
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
)

# The language instruction the service adds for each case language. Always
# set, because an unset output_language adds no language instruction.
OUTPUT_LANGUAGE = {"en": "English"}


def build_answer_key(
    records: Sequence[Mapping[str, Any]],
    family: str,
    *,
    required_min: int = REQUIRED_MIN_RECORDS,
) -> dict[str, Any]:
    """The answer key for ``family``, computed from the labels on ``records``.

    ``labels`` holds the family's label for every record, null included:
    the scorer reads the labels of the records a section cites, and a
    section of mostly unlabelled records is about something else.
    ``required`` lists the values with ``required_min`` records or more.
    ``protection`` gets ``urgent``, the ids of the urgent records,
    instead of ``labels`` and ``required``: it is scored on which of
    those records the answer cites, not on a label every record carries.
    ``groups`` maps each group to its records that also set one of the
    family's labels; every record sets ``theme``, so the ``themes`` key
    holds every group.

    An absent or empty part means "not scored", never "wrong".
    """
    labels = FAMILY_LABEL[family]

    key: dict[str, Any] = {"family": family}
    if family == "protection":
        key["urgent"] = sorted(
            record["id"] for record in records if record["metadata"].get("urgent_kind")
        )
    else:
        (label,) = labels
        by_record = {
            record["id"]: record["metadata"].get(label) or None for record in records
        }
        counts = Counter(value for value in by_record.values() if value)
        key["labels"] = by_record
        key["required"] = sorted(
            name for name, count in counts.items() if count >= required_min
        )

    ids_by_group: dict[str, list[str]] = {}
    for record in records:
        if not any(record["metadata"].get(label) for label in labels):
            continue
        for group in record["metadata"].get("groups") or []:
            ids_by_group.setdefault(group, []).append(record["id"])
    key["groups"] = {name: sorted(ids) for name, ids in sorted(ids_by_group.items())}

    return key


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


def send_order(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """``records``, newest ``created`` first, then by id — the EspoCRM send order.

    ``created`` timestamps are ``%Y-%m-%dT%H:%M:%SZ`` strings, which sort
    chronologically as plain text, so no parsing is needed.
    """
    by_id = sorted(records, key=lambda record: record["id"])
    return sorted(by_id, key=lambda record: record["metadata"]["created"], reverse=True)


def build_cases(
    records: Sequence[Mapping[str, Any]],
    prompts: Sequence[Any],
    read_at: datetime,
) -> list[dict[str, Any]]:
    """One case per English prompt, sending every one of ``records``.

    ``records`` must be as they stood at ``read_at``, so the send order
    and the answer key describe the same records; the caller fetches them
    at that version. Stops the whole build on the first prompt that
    cannot become a case, rather than uploading a partial case set.

    Raises
    ------
    SystemExit
        A prompt is tagged ``unplanted``, or its family (other than
        ``protection``) has no required value in its answer key.
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

        key = build_answer_key(records, family)
        if family != "protection" and not key["required"]:
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
                    "prompt_dataset": PROMPTS_DATASET,
                    "prompt_id": prompt_id,
                    "family": family,
                    "language": language,
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
    """Build the frequent case set from the live pool and prompts, and upload it."""
    args = _parse_args()
    load_env()
    read_at = datetime.now(UTC)

    langfuse = get_client()
    records = [
        record_from_item(item)
        for item in langfuse.get_dataset(RECORDS_DATASET, version=read_at).items
    ]
    prompts = langfuse.get_dataset(PROMPTS_DATASET, version=read_at).items

    cases = build_cases(records, prompts, read_at)

    for case in cases:
        key = case["expected_output"]
        # The protection key has no required values; it is scored on its
        # urgent records instead.
        required = ", ".join(key["required"]) if "required" in key else "-"
        n_records = len(case["input"]["record_ids"])
        print(
            f"{case['id']}: {n_records} records, family={key['family']}, "
            f"required: {required}"
        )

    report = upload_items(langfuse, FREQUENT_CASES_DATASET, cases, dry_run=args.dry_run)
    print(f"\nDataset: {FREQUENT_CASES_DATASET}{' (dry run)' if args.dry_run else ''}")
    print(
        f"{len(report.created)} created, {len(report.unchanged)} unchanged, {len(report.updated)} updated"
    )


if __name__ == "__main__":
    main()
