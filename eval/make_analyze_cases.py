r"""Build and upload the analyze-eval case set, one case per prompt in one language.

A case pairs one prompt with the pool's records in send order and an
answer key: what a correct response to that prompt should contain,
computed straight from the labels already on the pool records, so nobody
edits a key by hand and it can never drift from the pool it scores.

``build_answer_key`` and the scorers that read its output take a record
as ``{id, content, metadata}`` — the shape ``upload_pool.py`` uploads.
``record_from_item`` rebuilds that shape from what Langfuse hands back.

``--language`` picks the pool, the prompts and the case set. For a
language other than ``en``, ``check_translation`` first proves the
translated pool carries the same answers as the English one — this is
where a drifted translation is caught, on the live data.

Run::

    uv run python eval/make_analyze_cases.py --dry-run
    uv run python eval/make_analyze_cases.py
    uv run python eval/make_analyze_cases.py --language es --dry-run

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
    FREQUENT_CASES_DATASETS,
    LABEL_KEYS,
    PROMPTS_DATASET,
    RECORDS_DATASETS,
    REQUIRED_MIN_RECORDS,
)

# The language instruction the service adds for each case language. Always
# set, because an unset output_language adds no language instruction.
OUTPUT_LANGUAGE = {"en": "English", "es": "Spanish"}


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


def _relabel_key(
    key: Mapping[str, Any], base_by_id: Mapping[str, str]
) -> dict[str, Any]:
    """``key`` with every record id replaced by the id of the record it translates."""
    relabelled = dict(key)
    if "labels" in key:
        relabelled["labels"] = {
            base_by_id[record_id]: value for record_id, value in key["labels"].items()
        }
    if "urgent" in key:
        relabelled["urgent"] = sorted(base_by_id[r] for r in key["urgent"])
    relabelled["groups"] = {
        name: sorted(base_by_id[r] for r in ids)
        for name, ids in (key.get("groups") or {}).items()
    }
    return relabelled


def check_translation(
    records: Sequence[Mapping[str, Any]], base_records: Sequence[Mapping[str, Any]]
) -> None:
    """Check that ``records`` is ``base_records`` said again in another language.

    Proves the two acceptance criteria of #356 on the live data: every
    record translates exactly one base record (and every base record is
    translated), it copies that record's labels unchanged, and the answer
    key each family computes is therefore the same once the translated
    ids are mapped back. The last check is implied by the second, but it
    is the criterion itself and costs nothing, so it is asserted too.

    Raises
    ------
    SystemExit
        A record has no ``base_id``, names one that is not in
        ``base_records``, or shares one with another record; a base
        record has no translation; a label differs; or a family's answer
        key differs once relabelled.
    """
    base_by_id = {}
    for record in records:
        base_id = record["metadata"].get("base_id")
        if not base_id:
            raise SystemExit(f"{record['id']}: no base_id")
        if base_id in base_by_id.values():
            raise SystemExit(f"{record['id']}: base_id {base_id} is used twice")
        base_by_id[record["id"]] = base_id

    base = {record["id"]: record for record in base_records}
    unknown = sorted(set(base_by_id.values()) - set(base))
    if unknown:
        raise SystemExit(f"base_id(s) not in the base pool: {', '.join(unknown)}")
    untranslated = sorted(set(base) - set(base_by_id.values()))
    if untranslated:
        raise SystemExit(
            f"base record(s) with no translation: {', '.join(untranslated)}"
        )

    for record in records:
        original = base[base_by_id[record["id"]]]
        for label in LABEL_KEYS:
            mine, theirs = (
                record["metadata"].get(label),
                original["metadata"].get(label),
            )
            if label == "groups":
                mine, theirs = set(mine or []), set(theirs or [])
            if mine != theirs:
                raise SystemExit(
                    f"{record['id']}: {label} is {mine!r}, "
                    f"but {original['id']} has {theirs!r}"
                )

    for family in FAMILY_LABEL:
        relabelled = _relabel_key(build_answer_key(records, family), base_by_id)
        if relabelled != build_answer_key(base_records, family):
            raise SystemExit(f"{family}: the answer keys differ between the languages")


def record_from_item(item: Any) -> dict[str, Any]:
    """The pool record for a ``feedback/records-<language>-v1`` dataset item, in YAML shape.

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
    language: str = "en",
) -> list[dict[str, Any]]:
    """One case per ``language`` prompt, sending every one of ``records``.

    ``records`` must be as they stood at ``read_at``, so the send order
    and the answer key describe the same records; the caller fetches them
    at that version. Stops the whole build on the first prompt that
    cannot become a case, rather than uploading a partial case set.

    Raises
    ------
    SystemExit
        No prompt is in ``language``, or its prompt ids are not the same
        set as English's — a half-translated prompt set would otherwise
        build a short case set that looks complete. A prompt is tagged
        ``unplanted``, or its family (other than ``protection``) has no
        required value in its answer key.
    """
    in_language = [p for p in prompts if p.metadata["language"] == language]
    if not in_language:
        raise SystemExit(f"no prompt in {language}")
    if language != "en":
        mine = {p.metadata["prompt_id"] for p in in_language}
        english = {
            p.metadata["prompt_id"] for p in prompts if p.metadata["language"] == "en"
        }
        if mine != english:
            missing = ", ".join(sorted(english - mine)) or "-"
            extra = ", ".join(sorted(mine - english)) or "-"
            raise SystemExit(
                f"{language} prompts do not match the English set: "
                f"missing {missing}, extra {extra}"
            )

    record_ids = [record["id"] for record in send_order(records)]
    version = read_at.isoformat()

    cases = []
    for prompt in in_language:
        prompt_id = prompt.metadata["prompt_id"]
        family = prompt.metadata["family"]

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
                    "records_dataset": RECORDS_DATASETS[language],
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
        "--language",
        default="en",
        choices=sorted(RECORDS_DATASETS),
        help="Language of the pool, the prompts and the case set. Default: en.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check, but write nothing to Langfuse.",
    )
    return parser.parse_args()


def _load_records(
    langfuse: Any, dataset: str, read_at: datetime
) -> list[dict[str, Any]]:
    return [
        record_from_item(item)
        for item in langfuse.get_dataset(dataset, version=read_at).items
    ]


def main() -> None:
    """Build the frequent case set from the live pool and prompts, and upload it."""
    args = _parse_args()
    load_env()
    read_at = datetime.now(UTC)
    language = args.language

    langfuse = get_client()
    records = _load_records(langfuse, RECORDS_DATASETS[language], read_at)
    prompts = langfuse.get_dataset(PROMPTS_DATASET, version=read_at).items

    if language != "en":
        base_dataset = RECORDS_DATASETS["en"]
        check_translation(records, _load_records(langfuse, base_dataset, read_at))
        print(f"checked {len(records)} records against {base_dataset}\n")

    cases = build_cases(records, prompts, read_at, language)

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

    dataset = FREQUENT_CASES_DATASETS[language]
    report = upload_items(langfuse, dataset, cases, dry_run=args.dry_run)
    print(f"\nDataset: {dataset}{' (dry run)' if args.dry_run else ''}")
    print(
        f"{len(report.created)} created, {len(report.unchanged)} unchanged, {len(report.updated)} updated"
    )


if __name__ == "__main__":
    main()
