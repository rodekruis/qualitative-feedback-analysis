r"""Check and upload the analyze feedback pool to ``feedback/records-en-v1``.

Reads a working YAML file of pool records — domain data, not code, so it
stays out of git (``.corpus_work/``) — checks it against the planted counts
in ``PLANTED``, and upserts it via ``upload_items()``. Each record is
``{id, content, metadata}``, with ``created`` and every label in
``metadata`` — the shape of ``fixtures/analyze_corpus.yaml``. Each label
records one known fact about the record — its theme, an unmet need it
reports, a vulnerable group it is about, and so on — set by whoever wrote
the record's text, never inferred later.

With ``--vocab``, it also checks a working YAML file of vocabulary entries
against the records and uploads it to ``feedback/vocab-v1``, after the
records. A downstream scorer counts a fact as reported when one of these
keywords appears in a model's answer, matched by ``_common.contains_term``.

Prerequisites
-------------
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` — the
  Langfuse project to upload to.

Run::

    uv run python eval/upload_pool.py \\
      --input .corpus_work/analyze-pool/records-en-v1.yaml --dry-run

    uv run python eval/upload_pool.py \\
      --input .corpus_work/analyze-pool/records-en-v1.yaml \\
      --vocab .corpus_work/analyze-pool/vocab-v1.yaml --dry-run
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langfuse import get_client

from _common import contains_term, load_env, upload_items
from pool_spec import (
    BANNED_WORDS,
    CREATED_FORMAT,
    DECOYS,
    PLANTED,
    RECORDS_DATASET,
    STOPLIST,
    VOCAB_DATASET,
    VOCAB_LABELS,
    WINDOW_END,
    WINDOW_START,
)

ID_PATTERN = re.compile(r"^en-\d{4}$")

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
    "keywords",
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
        "--vocab",
        type=Path,
        help=(
            "YAML file of vocabulary entries. When given, checks it against "
            "the records and uploads it to feedback/vocab-v1, after the "
            "records."
        ),
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


def validate_pool(
    records: list[dict[str, Any]],
    *,
    planted: Mapping[str, Mapping[str, int]] = PLANTED,
    decoys: int = DECOYS,
    final: bool = False,
) -> None:
    """Check the pool records before any upload.

    ``planted`` and ``decoys`` default to the real distribution; tests pass
    smaller values. ``final=False`` skips text checks on records with no
    text and prints how many those are. ``final=True`` requires text and
    ``review_status`` on every record, and ``review_status: "reviewed"``
    on every urgent, decoy or group record.

    Raises
    ------
    SystemExit
        An id is not shaped ``en-NNNN``, or repeats; a record is missing a
        metadata field; a ``planted`` count is wrong; the decoy count is
        wrong, two urgent records share an ``urgent_kind``, or one is not
        themed ``protection``; a ``created`` falls outside the window; a
        record with text has a bad length, no ``facts``, a banned word,
        or too few keywords; or ``final`` is set and a record lacks text,
        ``review_status``, or a required ``reviewed`` value.
    """
    ids = [r["id"] for r in records]
    bad_ids = sorted(i for i in set(ids) if not ID_PATTERN.match(i))
    if bad_ids:
        raise SystemExit(f"Ids not shaped en-NNNN: {', '.join(bad_ids)}")
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
        for word in BANNED_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", content, re.IGNORECASE):
                raise SystemExit(f"{r['id']}: content uses the banned word {word!r}")
        is_urgent_or_decoy = r["metadata"].get("urgent_kind") or r["metadata"].get(
            "decoy"
        )
        if is_urgent_or_decoy and len(r["metadata"].get("keywords") or []) < 3:
            raise SystemExit(
                f"{r['id']}: urgent/decoy record with text needs 3 or more keywords"
            )


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


def load_vocab(path: Path) -> list[dict[str, Any]]:
    """Read the working YAML file of vocabulary entries."""
    entries = yaml.safe_load(path.read_text())
    return entries or []


def _vocab_id(entry: Mapping[str, Any]) -> str:
    """The bare id of a vocabulary entry: ``<kind>:<name>``, e.g. ``group:older_people``."""
    return f"{entry['kind']}:{entry['name']}"


def _shared_keyword(
    owners: list[tuple[str, list[str]]],
) -> tuple[str, str, str] | None:
    """The first keyword two owners share, as ``(keyword, first owner, second owner)``."""
    seen: dict[str, str] = {}
    for name, keywords in owners:
        for keyword in keywords:
            if keyword in seen and seen[keyword] != name:
                return keyword, seen[keyword], name
            seen[keyword] = name
    return None


def validate_vocab(
    entries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    vocab_labels: Mapping[str, str] = VOCAB_LABELS,
    stoplist: Sequence[str] = STOPLIST,
) -> None:
    """Check the vocabulary entries against the pool records, before any upload.

    ``vocab_labels`` and ``stoplist`` default to the real ones; tests pass
    smaller values.

    Raises
    ------
    SystemExit
        A value of a ``vocab_labels`` label used in the records has no
        matching entry, or an entry's value matches no record; an entry
        has fewer than 3 ``keywords.en``, or a group entry fewer than 3
        ``issue_keywords.en``; a keyword, in the vocabulary or on a
        record, contains a ``stoplist`` word; two group entries, or two
        urgent records, share a keyword; or an urgent record's keyword
        appears in a decoy's text.
    """
    entry_names_by_kind: dict[str, set[str]] = {}
    for entry in entries:
        entry_names_by_kind.setdefault(entry["kind"], set()).add(entry["name"])

    for field_name, kind in vocab_labels.items():
        if field_name == "groups":
            record_values = {
                g for r in records for g in r["metadata"].get("groups") or []
            }
        else:
            record_values = {
                r["metadata"][field_name]
                for r in records
                if r["metadata"].get(field_name)
            }
        entry_values = entry_names_by_kind.get(kind, set())
        missing = sorted(record_values - entry_values)
        if missing:
            raise SystemExit(f"{kind}: no vocabulary entry for {', '.join(missing)}")
        orphans = sorted(entry_values - record_values)
        if orphans:
            raise SystemExit(
                f"{kind}: entry for {', '.join(orphans)} matches no record"
            )

    for entry in entries:
        entry_id = _vocab_id(entry)
        if len(entry.get("keywords", {}).get("en") or []) < 3:
            raise SystemExit(f"{entry_id}: fewer than 3 keywords.en")
        if entry["kind"] == "group":
            if len(entry.get("issue_keywords", {}).get("en") or []) < 3:
                raise SystemExit(f"{entry_id}: fewer than 3 issue_keywords.en")

    vocab_keywords = [
        (keyword, _vocab_id(entry))
        for entry in entries
        for field in ("keywords", "issue_keywords")
        for keyword in entry.get(field, {}).get("en") or []
    ]
    record_keywords = [
        (keyword, r["id"])
        for r in records
        for keyword in r["metadata"].get("keywords") or []
    ]
    for keyword, source in vocab_keywords + record_keywords:
        stoplisted = next(
            (word for word in stoplist if contains_term(keyword, word)), None
        )
        if stoplisted:
            raise SystemExit(
                f"{source}: keyword {keyword!r} contains the stoplisted word {stoplisted!r}"
            )

    group_owners = [
        (entry["name"], entry.get("keywords", {}).get("en") or [])
        for entry in entries
        if entry["kind"] == "group"
    ]
    if shared := _shared_keyword(group_owners):
        keyword, first, second = shared
        raise SystemExit(f"group keyword {keyword!r} shared by {first} and {second}")

    urgent = [r for r in records if r["metadata"].get("urgent_kind")]
    urgent_owners = [(r["id"], r["metadata"].get("keywords") or []) for r in urgent]
    if shared := _shared_keyword(urgent_owners):
        keyword, first, second = shared
        raise SystemExit(f"urgent keyword {keyword!r} shared by {first} and {second}")

    decoys = [r for r in records if r["metadata"].get("decoy")]
    for r in urgent:
        for keyword in r["metadata"].get("keywords") or []:
            hit = next(
                (
                    d["id"]
                    for d in decoys
                    if contains_term(d.get("content") or "", keyword)
                ),
                None,
            )
            if hit:
                raise SystemExit(
                    f"{r['id']}'s urgent keyword {keyword!r} appears in decoy {hit}"
                )


def build_vocab_items(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map each vocabulary entry to the shape ``upload_items()`` expects.

    ``input`` carries the keyword lists — the payload a model's answer
    gets matched against. ``kind`` and ``name``, which together make the
    bare id, stay in the item's own ``metadata`` as plain tags for
    browsing in the Langfuse UI.
    """
    items = []
    for entry in entries:
        input_: dict[str, Any] = {"keywords": entry["keywords"]}
        if "issue_keywords" in entry:
            input_["issue_keywords"] = entry["issue_keywords"]
        items.append(
            {
                "id": _vocab_id(entry),
                "input": input_,
                "expected_output": None,
                "metadata": {"kind": entry["kind"], "name": entry["name"]},
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
    """Validate and upload the pool, and optionally the vocabulary, named on the command line."""
    args = _parse_args()
    load_env()

    records = load_pool(args.input)
    validate_pool(records, final=not args.dry_run)
    items = build_items(records)
    langfuse = get_client()
    _upload(langfuse, RECORDS_DATASET, items, dry_run=args.dry_run)

    if args.vocab:
        vocab_entries = load_vocab(args.vocab)
        validate_vocab(vocab_entries, records)
        vocab_items = build_vocab_items(vocab_entries)
        print()
        _upload(langfuse, VOCAB_DATASET, vocab_items, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
