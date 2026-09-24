r"""Print which words the anonymizer would mask in the analyze eval pool.

Helper script (run manually, not in CI). Every record's text and the
analyst prompt are anonymised together in one placeholder namespace
before either reaches the model — see
``qfa.services.llm_call_executor.LLMCallExecutor.anonymize_records_and_prompt``.
If the anonymizer masks a word that a downstream score depends on — a
keyword, a group word, anything the ground truth relies on — the score
for that record falls even though the model behaved correctly on what
it was actually shown.

This script surfaces that risk before the pool goes anywhere near a real
run: for each prompt, it anonymises that prompt together with every
record's text — the same call shape the service makes — and prints which
original words got replaced with a placeholder, in the prompt or in a
record. It writes nothing back to either file.

Run::

    uv run python scripts/check_pool_masking.py \\
      --input .corpus_work/analyze-pool/records-en-v1.yaml \\
      --prompts .corpus_work/analyze-pool/prompts-v1.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print which words the anonymizer masks in the analyze eval pool."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="YAML file of pool records (the shape upload_pool.py reads).",
    )
    parser.add_argument(
        "--prompts",
        required=True,
        type=Path,
        help="YAML file of prompt records (the shape upload_prompts.py reads).",
    )
    return parser.parse_args(argv)


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    """Read a YAML file holding a list of records or prompts."""
    return yaml.safe_load(path.read_text()) or []


def _masked_words(redacted_text: str, mapping: dict[str, str]) -> list[str]:
    """Original values from ``mapping`` whose placeholder appears in ``redacted_text``.

    ``mapping`` is shared across a whole ``anonymize_batch`` call, so this
    is what narrows it back down to the words masked in one specific text.
    """
    return [
        original
        for placeholder, original in mapping.items()
        if placeholder in redacted_text
    ]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: anonymise each prompt with every record, print what got masked."""
    from qfa.adapters.presidio_anonymizer import PresidioAnonymizer

    args = _parse_args(argv)
    records = _load_yaml_list(args.input)
    prompts = _load_yaml_list(args.prompts)

    anonymizer = PresidioAnonymizer()
    masked_by_record: dict[str, set[str]] = {}
    any_masked = False

    for prompt_record in prompts:
        texts = (prompt_record["prompt"], *(r["content"] for r in records))
        redacted, mapping = anonymizer.anonymize_batch(texts)

        prompt_masked = _masked_words(redacted[0], mapping)
        if prompt_masked:
            any_masked = True
            print(
                f"prompt {prompt_record['id']}: {', '.join(sorted(set(prompt_masked)))}"
            )

        for record, redacted_content in zip(records, redacted[1:], strict=True):
            record_masked = _masked_words(redacted_content, mapping)
            if record_masked:
                masked_by_record.setdefault(record["id"], set()).update(record_masked)

    for record_id in sorted(masked_by_record):
        any_masked = True
        print(f"record {record_id}: {', '.join(sorted(masked_by_record[record_id]))}")

    if not any_masked:
        print("No masked words found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
