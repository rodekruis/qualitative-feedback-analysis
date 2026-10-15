r"""Upload the analyze prompts to the Langfuse dataset ``analyze/prompts-v1``.

Reads a working YAML file of prompt records — domain data, not code, so it
stays out of git (``.corpus_work/``) — checks it, and upserts it via
``upload_items()``. Each record is
``{id, prompt, metadata: {prompt_id, language, twin_id, family, source,
supplied_by, supplied_on, use_case}}``. ``id`` is ``<prompt_id>-<lang>``,
for example ``P03-en``.

Prerequisites
-------------
- ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST`` — the
  Langfuse project to upload to.

Run::

    uv run python eval/upload_prompts.py --dataset analyze/prompts-v1 \\
      --input .corpus_work/analyze-pool/prompts-v1.yaml --dry-run
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from langfuse import get_client

from _common import load_env, upload_items
from pool_spec import FAMILY_LABEL

UNPLANTED_FAMILY = "unplanted"

REQUIRED_METADATA_FIELDS = (
    "prompt_id",
    "language",
    "twin_id",
    "family",
    "source",
    "supplied_by",
    "supplied_on",
    "use_case",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload the analyze prompts to a Langfuse dataset.",
        epilog=(
            "Example:\n"
            "  uv run python eval/upload_prompts.py --dataset analyze/prompts-v1 \\\n"
            "    --input .corpus_work/analyze-pool/prompts-v1.yaml --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Name of the Langfuse dataset, e.g. analyze/prompts-v1.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="YAML file of prompt records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check, but write nothing to Langfuse.",
    )
    return parser.parse_args()


def load_prompts(path: Path) -> list[dict[str, Any]]:
    """Read the working YAML file of prompt records."""
    records = yaml.safe_load(path.read_text())
    return records or []


def validate_prompts(records: list[dict[str, Any]]) -> None:
    """Check the prompt records the ticket requires, before any upload.

    Raises
    ------
    SystemExit
        A bare id repeats, a prompt's family is neither a known family nor
        ``"unplanted"``, or a metadata field is missing.
    """
    ids = [record["id"] for record in records]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"Duplicate prompt ids: {', '.join(duplicates)}")

    for record in records:
        metadata = record.get("metadata") or {}
        missing = [field for field in REQUIRED_METADATA_FIELDS if field not in metadata]
        if missing:
            raise SystemExit(
                f"{record['id']}: missing metadata field(s): {', '.join(missing)}"
            )

        family = metadata["family"]
        if family != UNPLANTED_FAMILY and family not in FAMILY_LABEL:
            raise SystemExit(
                f"{record['id']}: unknown family {family!r}. Use one of "
                f"{sorted(FAMILY_LABEL)} or {UNPLANTED_FAMILY!r}."
            )


def build_items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map each prompt record to the shape ``upload_items()`` expects."""
    return [
        {
            "id": record["id"],
            "input": record["prompt"],
            "expected_output": None,
            "metadata": record["metadata"],
        }
        for record in records
    ]


def main() -> None:
    """Validate and upload the prompts named on the command line."""
    args = _parse_args()
    load_env()

    records = load_prompts(args.input)
    validate_prompts(records)
    items = build_items(records)

    langfuse = get_client()
    report = upload_items(
        langfuse,
        args.dataset,
        items,
        dry_run=args.dry_run,
    )

    print(f"Dataset: {args.dataset}{' (dry run)' if args.dry_run else ''}")
    print(
        f"{len(report.created)} created, {len(report.unchanged)} unchanged, {len(report.updated)} updated"
    )
    if report.extra:
        print(f"{len(report.extra)} in Langfuse but not in the file (not deleted)")


if __name__ == "__main__":
    main()
