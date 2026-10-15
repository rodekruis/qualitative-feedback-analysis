r"""Check and upload one language's analyze feedback pool to its Langfuse dataset.

Reads a working YAML file of pool records — domain data, not code, so it
stays out of git (``.corpus_work/``) — checks it against the planted counts
in ``PLANTED``, and upserts it via ``upload_items()``. Each record is
``{id, content, metadata}``, with ``created`` and every label in
``metadata`` — the shape of ``fixtures/analyze_corpus.yaml``. Each label
records one known fact about the record — its theme, an unmet need it
reports, a vulnerable group it is about, and so on — set by whoever wrote
the record's text, never inferred later.

``--language`` picks the dataset (``RECORDS_DATASETS``) and the id shape.
A translated pool is the English pool said again in another language:
same labels, same counts, a new id, and ``base_id`` naming the English
record it translates.

Prerequisites
-------------
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` — the
  Langfuse project to upload to.

Run::

    uv run python eval/upload_pool.py \\
      --input .corpus_work/analyze-pool/records-en-v1.yaml --dry-run
    uv run python eval/upload_pool.py --language es \\
      --input .corpus_work/analyze-pool/records-es-v1.yaml --dry-run
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langdetect import DetectorFactory, LangDetectException, detect
from langfuse import get_client

from _common import load_env, upload_items
from pool_spec import (
    BANNED_WORDS,
    CREATED_FORMAT,
    DECOYS,
    PLANTED,
    RECORDS_DATASETS,
    WINDOW_END,
    WINDOW_START,
)

# langdetect samples at random; without a fixed seed the same record can
# warn on one run and not the next.
DetectorFactory.seed = 0

BASE_ID_PATTERN = re.compile(r"^en-\d{4}$")

EXPECTED_METADATA_KEYS = (
    "theme",
    "need",
    "complaint_about",
    "rumour",
    "suggestion",
    "praise_about",
    "info_request",
    "urgent_kind",
    "decoy",
    "promise_gap",
    "access_barrier",
    "groups",
    "facts",
    "language",
    "created",
    "gen_model",
    "gen_date",
    "review_status",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check and upload the analyze feedback pool to a Langfuse dataset.",
        epilog=(
            "Example:\n"
            "  uv run python eval/upload_pool.py \\\n"
            "    --input .corpus_work/analyze-pool/records-en-v1.yaml --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="YAML file of pool records.",
    )
    parser.add_argument(
        "--language",
        default="en",
        choices=sorted(RECORDS_DATASETS),
        help="Language of the pool, which picks the dataset. Default: en.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check, but write nothing to Langfuse.",
    )
    return parser.parse_args()


def load_pool(path: Path) -> list[dict[str, Any]]:
    """Read the working YAML file of pool records."""
    records = yaml.safe_load(path.read_text())
    return records or []


def _field_counts(records: list[dict[str, Any]], field_name: str) -> Counter[str]:
    return Counter(
        r["metadata"][field_name] for r in records if r["metadata"].get(field_name)
    )


def _group_counts(records: list[dict[str, Any]]) -> Counter[str]:
    return Counter(g for r in records for g in r["metadata"].get("groups") or [])


def _parse_created(value: str) -> datetime:
    return datetime.strptime(value, CREATED_FORMAT)


def _needs_reviewed(metadata: Mapping[str, Any]) -> bool:
    return bool(
        metadata.get("urgent_kind") or metadata.get("decoy") or metadata.get("groups")
    )


def _check_translation_links(records: list[dict[str, Any]]) -> None:
    """Check that every record names exactly one English record in ``base_id``."""
    seen: dict[str, str] = {}
    for r in records:
        base_id = r["metadata"].get("base_id")
        if not base_id:
            raise SystemExit(f"{r['id']}: no base_id")
        if not BASE_ID_PATTERN.match(base_id):
            raise SystemExit(f"{r['id']}: base_id {base_id!r} is not shaped en-NNNN")
        if base_id in seen:
            raise SystemExit(
                f"{r['id']} and {seen[base_id]} share the base_id {base_id}"
            )
        seen[base_id] = r["id"]


def _warn_on_wrong_language(records: list[dict[str, Any]], language: str) -> None:
    """Print a warning for each record whose text does not read as ``language``.

    A warning, never an exit: ``langdetect`` misfires on a short record
    full of proper nouns, and a blocked upload with no override would be
    worse than a false alarm. ``language_match`` in the eval harness is
    the scored version of this question.
    """
    for r in records:
        content = r.get("content") or ""
        if not content:
            continue
        try:
            detected = detect(content)
        except LangDetectException:
            detected = "unknown"
        if detected != language:
            print(f"warning: {r['id']} reads as {detected}, not {language}")


def validate_pool(
    records: list[dict[str, Any]],
    *,
    planted: Mapping[str, Mapping[str, int]] = PLANTED,
    decoys: int = DECOYS,
    final: bool = False,
    language: str = "en",
) -> None:
    """Check the pool records before any upload.

    ``planted`` and ``decoys`` default to the real distribution; tests pass
    smaller values. ``final=False`` skips text checks on records with no
    text and prints how many those are. ``final=True`` requires text and
    ``review_status`` on every record, and ``review_status: "reviewed"``
    on every urgent, decoy or group record.

    ``language`` sets the id shape, the banned words, and the value every
    record's ``language`` must carry. A pool that is not ``en`` is a
    translation, so every record must also name the English record it
    translates in ``base_id``. Prints — never raises — for a record whose
    text does not read as ``language``.

    Raises
    ------
    SystemExit
        An id is not shaped ``<language>-NNNN``, or repeats; a record is
        missing a metadata field or carries the wrong ``language``; a
        translated record has no ``base_id``, a misshapen one, or one
        another record already uses; a ``planted`` count is wrong; the
        decoy count is wrong, two urgent records share an ``urgent_kind``,
        or one is not themed ``protection``; a ``created`` falls outside
        the window; a record with text has a bad length, no ``facts``, or
        a banned word; or ``final`` is set and a record lacks text,
        ``review_status``, or a required ``reviewed`` value.
    """
    id_pattern = re.compile(rf"^{re.escape(language)}-\d{{4}}$")
    ids = [r["id"] for r in records]
    bad_ids = sorted(i for i in set(ids) if not id_pattern.match(i))
    if bad_ids:
        raise SystemExit(f"Ids not shaped {language}-NNNN: {', '.join(bad_ids)}")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"Duplicate record ids: {', '.join(duplicates)}")

    for r in records:
        missing = [
            key for key in EXPECTED_METADATA_KEYS if key not in r.get("metadata", {})
        ]
        if missing:
            raise SystemExit(
                f"{r['id']}: missing metadata field(s): {', '.join(missing)}"
            )
        if r["metadata"]["language"] != language:
            raise SystemExit(
                f"{r['id']}: language is {r['metadata']['language']!r}, want {language!r}"
            )

    if language != "en":
        _check_translation_links(records)

    for field_name, counts in planted.items():
        actual = (
            _group_counts(records)
            if field_name == "groups"
            else _field_counts(records, field_name)
        )
        if actual != counts:
            raise SystemExit(
                f"{field_name}: expected {dict(counts)}, got {dict(actual)}"
            )

    urgent = [r for r in records if r["metadata"].get("urgent_kind")]
    decoy_records = [r for r in records if r["metadata"].get("decoy")]
    if len(decoy_records) != decoys:
        raise SystemExit(f"Expected {decoys} decoys, got {len(decoy_records)}")
    kinds = [r["metadata"]["urgent_kind"] for r in urgent]
    if len(set(kinds)) != len(kinds):
        raise SystemExit("Two urgent records share the same urgent_kind")
    mislabeled = [
        r["id"]
        for r in urgent + decoy_records
        if r["metadata"]["theme"] != "protection"
    ]
    if mislabeled:
        raise SystemExit(
            f"Urgent/decoy record(s) not themed protection: {', '.join(mislabeled)}"
        )

    out_of_window = [
        r["id"]
        for r in records
        if not (WINDOW_START <= _parse_created(r["metadata"]["created"]) <= WINDOW_END)
    ]
    if out_of_window:
        raise SystemExit(
            f"created outside 2026-06-01..2026-08-31: {', '.join(out_of_window)}"
        )

    empty_text = [r["id"] for r in records if not (r.get("content") or "")]
    if final:
        if empty_text:
            raise SystemExit(
                f"{len(empty_text)} record(s) have no text: {', '.join(empty_text)}"
            )
        missing_review = [
            r["id"] for r in records if r["metadata"].get("review_status") is None
        ]
        if missing_review:
            raise SystemExit(f"missing review_status: {', '.join(missing_review)}")
        not_reviewed = [
            r["id"]
            for r in records
            if _needs_reviewed(r["metadata"])
            and r["metadata"].get("review_status") != "reviewed"
        ]
        if not_reviewed:
            raise SystemExit(
                "urgent/decoy/group record(s) need "
                f'review_status: "reviewed": {", ".join(not_reviewed)}'
            )
    elif empty_text:
        print(f"{len(empty_text)} records have no text yet")

    for r in records:
        content = r.get("content") or ""
        if not content:
            continue
        if not (60 <= len(content) <= 1200):
            raise SystemExit(
                f"{r['id']}: content is {len(content)} characters, want 60-1200"
            )
        if not r["metadata"].get("facts"):
            raise SystemExit(f"{r['id']}: has text but no facts")
        for word in BANNED_WORDS[language]:
            if re.search(rf"\b{re.escape(word)}\b", content, re.IGNORECASE):
                raise SystemExit(f"{r['id']}: content uses the banned word {word!r}")

    _warn_on_wrong_language(records, language)


def build_items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map each pool record to the shape ``upload_items()`` expects.

    ``input`` is the record as the API accepts it — ``{id, content,
    metadata: {created}}`` — so ``scripts/stress_analyze.py`` can send it
    unchanged. Every other label stays in the item's own ``metadata``.
    """
    items = []
    for r in records:
        metadata = dict(r["metadata"])
        created = metadata.pop("created")
        items.append(
            {
                "id": r["id"],
                "input": {
                    "id": r["id"],
                    "content": r["content"],
                    "metadata": {"created": created},
                },
                "expected_output": None,
                "metadata": metadata,
            }
        )
    return items


def _upload(
    langfuse: Any, dataset: str, items: list[dict[str, Any]], *, dry_run: bool
) -> None:
    """Upload ``items`` and print the same three-line report for any dataset."""
    report = upload_items(langfuse, dataset, items, dry_run=dry_run)
    print(f"Dataset: {dataset}{' (dry run)' if dry_run else ''}")
    print(
        f"{len(report.created)} created, {len(report.unchanged)} unchanged, {len(report.updated)} updated"
    )
    if report.extra:
        print(f"{len(report.extra)} in Langfuse but not in the file (not deleted)")


def main() -> None:
    """Validate and upload the pool named on the command line."""
    args = _parse_args()
    load_env()

    records = load_pool(args.input)
    validate_pool(records, final=not args.dry_run, language=args.language)
    items = build_items(records)
    langfuse = get_client()
    _upload(langfuse, RECORDS_DATASETS[args.language], items, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
