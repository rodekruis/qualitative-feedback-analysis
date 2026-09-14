r"""Benchmark ``PresidioAnonymizer.anonymize_batch`` throughput.

Helper script (run manually, not in CI), companion to
``scripts/stress_analyze.py``. Loads a sample from
``fixtures/analyze_corpus.yaml``, runs it through ``anonymize_batch`` once,
and prints wall-clock and records/sec — the repeatable form of the
``anonymisation: N record(s) in %.2fs`` log line
(``docs/operations/observability.md``).

``--check`` additionally runs the same sample through a forced-serial
(``max_workers=1``) adapter and asserts the two results are identical
(redacted texts *and* mapping), which is the acceptance criterion for the
parallel detection pass in :mod:`qfa.adapters.presidio_anonymizer`: any
``max_workers`` must produce byte-identical output.

Run::

    uv run python scripts/bench_anonymize.py --records 602 --check
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "fixtures" / "analyze_corpus.yaml"

# scripts/ isn't a package; import its sibling directly.
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"YAML corpus to sample from (default {DEFAULT_CORPUS})",
    )
    parser.add_argument(
        "--records",
        type=int,
        default=602,
        help="Number of records to sample (default 602, the prd log line's size).",
    )
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Override ANONYMIZATION_MAX_WORKERS default for the timed run.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Also run the sample through a max_workers=1 adapter and assert"
            " the result is identical to the timed run."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: sample the corpus, time one ``anonymize_batch`` call."""
    from stress_analyze import load_sample  # local import: see sys.path.insert above

    from qfa.adapters.presidio_anonymizer import PresidioAnonymizer

    args = _parse_args(argv)
    sample = load_sample(args.corpus, args.records, args.seed)
    texts = tuple(record["content"] for record in sample)

    kwargs = {} if args.max_workers is None else {"max_workers": args.max_workers}
    anonymizer = PresidioAnonymizer(**kwargs)

    start = time.perf_counter()
    redacted, mapping = anonymizer.anonymize_batch(texts)
    elapsed = time.perf_counter() - start

    print(
        f"{len(texts)} record(s) in {elapsed:.2f}s "
        f"({len(texts) / elapsed:.1f} records/sec, "
        f"max_workers={anonymizer._max_workers}, "
        f"batch_size={anonymizer._batch_size}, {len(mapping)} entities)"
    )

    if not args.check:
        return 0

    serial = PresidioAnonymizer(max_workers=1)
    serial_start = time.perf_counter()
    serial_redacted, serial_mapping = serial.anonymize_batch(texts)
    serial_elapsed = time.perf_counter() - serial_start
    print(f"serial (max_workers=1) reference: {serial_elapsed:.2f}s")

    if (redacted, mapping) != (serial_redacted, serial_mapping):
        print("--check FAILED: output differs from the serial reference.")
        return 1

    print("--check passed: output matches the serial reference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
