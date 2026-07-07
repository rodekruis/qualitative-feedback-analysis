"""HDBSCAN clustering + token-budget chunking for hierarchical analysis.

Deterministic ``services`` logic (no port): turns dense embedding vectors
into a set of :class:`~qfa.domain.chunk_models.Chunk` objects for the
map step. HDBSCAN needs no preset cluster count, no fixed ``eps``, and
labels outliers as noise (``-1``).

Two invariants, both unit-tested:

1. **Full coverage** — the union of all chunk records equals the input
   set; no record is dropped (outliers go into uncategorised chunks).
2. **Budget** — no returned chunk exceeds ``max_total_tokens``; an
   over-budget group is split into budget-sized sub-chunks.
"""

import logging
import math

import hdbscan
import numpy as np

from qfa.domain.chunk_models import Chunk
from qfa.domain.models import FeedbackRecordModel

logger = logging.getLogger(__name__)


def _estimate_tokens(
    records: tuple[FeedbackRecordModel, ...], chars_per_token: int
) -> int:
    """Estimate tokens for a group of records by total text length."""
    return sum(len(r.content) for r in records) // chars_per_token


def _iso_date_prefix(raw: object) -> str | None:
    """Return a lexically-sortable ISO date prefix, or ``None`` if absent.

    Requires at least a ``YYYY-MM`` prefix. ISO-8601 strings sort correctly
    lexically (``"2024-01-05T10:00" < "2024-01-06"``), so we deliberately
    avoid parsing to ``datetime`` — the raw string is its own sort key, and
    intra-day ordering still works. Anything that is not a string with a
    plausible date prefix returns ``None``.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) < 7 or text[4] != "-":
        return None
    if not (text[:4].isdigit() and text[5:7].isdigit()):
        return None
    return text


def _sort_by_date(
    records: tuple[FeedbackRecordModel, ...],
) -> tuple[FeedbackRecordModel, ...]:
    """Order records chronologically by their ``created`` metadata.

    Dated records come first, ascending; undated or unparseable-date records
    sort last. Sorting is stable, so records sharing a key (and all the
    undated ones) keep their original relative order — which is what makes
    chunk membership deterministic and runs reproducible.
    """

    def key(record: FeedbackRecordModel) -> tuple[bool, str]:
        prefix = _iso_date_prefix(record.metadata.created)
        return (prefix is None, prefix or "")

    return tuple(sorted(records, key=key))


def _balanced_contiguous_split(
    records: tuple[FeedbackRecordModel, ...], n_parts: int
) -> list[tuple[FeedbackRecordModel, ...]]:
    """Split records into ``n_parts`` contiguous, near-equal-count groups.

    The first ``len % n_parts`` groups get one extra record. Contiguity
    matters: records are pre-sorted by date, so contiguous slices are
    time-windows — a "lightest-bin" balancer would shuffle them out of order.
    """
    base, extra = divmod(len(records), n_parts)
    groups: list[tuple[FeedbackRecordModel, ...]] = []
    start = 0
    for part in range(n_parts):
        size = base + (1 if part < extra else 0)
        if size == 0:
            continue
        groups.append(records[start : start + size])
        start += size
    return groups


def _split_to_budget(
    records: tuple[FeedbackRecordModel, ...],
    *,
    max_total_tokens: int,
    chars_per_token: int,
) -> list[tuple[FeedbackRecordModel, ...]]:
    """Split records into roughly equal contiguous groups that fit the budget.

    Sizing is balanced, not greedy-fill-then-remainder: we start from the
    fewest parts that could fit the budget on average
    (``ceil(total / budget)``) and grow the part count only if a balanced
    split still has a part over budget. This flattens the tail — every group
    is about the same size — while preserving the hard budget guarantee. A
    single record larger than the budget still occupies its own group (it
    cannot be split further here; the orchestrator's per-chunk recursion
    handles it), which is why the growth loop stops once parts hold one record.
    """
    budget_chars = max_total_tokens * chars_per_token
    total_chars = sum(len(r.content) for r in records)
    n_parts = max(1, math.ceil(total_chars / budget_chars)) if budget_chars else 1

    while True:
        groups = _balanced_contiguous_split(records, n_parts)
        fits = all(sum(len(r.content) for r in g) <= budget_chars for g in groups)
        if fits or n_parts >= len(records):
            return groups
        n_parts += 1


def _budgeted_chunks(
    records: tuple[FeedbackRecordModel, ...],
    *,
    label: int,
    is_uncategorised: bool,
    max_total_tokens: int,
    chars_per_token: int,
) -> list[Chunk]:
    """Build one or more budget-sized chunks for a single cluster/noise group."""
    if _estimate_tokens(records, chars_per_token) <= max_total_tokens:
        return [Chunk(label=label, is_uncategorised=is_uncategorised, records=records)]
    return [
        Chunk(label=label, is_uncategorised=is_uncategorised, records=group)
        for group in _split_to_budget(
            records,
            max_total_tokens=max_total_tokens,
            chars_per_token=chars_per_token,
        )
    ]


def cluster_records(
    *,
    records: tuple[FeedbackRecordModel, ...],
    vectors: tuple[tuple[float, ...], ...],
    min_cluster_size: int,
    max_total_tokens: int,
    chars_per_token: int,
    metric: str = "euclidean",
    target_chunk_tokens: int | None = None,
) -> tuple[Chunk, ...]:
    """Cluster records by their embedding vectors into budget-sized chunks.

    Parameters
    ----------
    records : tuple[FeedbackRecordModel, ...]
        The records to cluster (same order/length as ``vectors``).
    vectors : tuple[tuple[float, ...], ...]
        Dense embedding vector per record.
    min_cluster_size : int
        HDBSCAN ``min_cluster_size``.
    max_total_tokens : int
        Per-chunk token *ceiling* — the hard limit of what one LLM call can
        hold. No returned chunk ever exceeds it.
    chars_per_token : int
        Char-to-token conversion ratio for the budget estimate.
    metric : str
        HDBSCAN distance metric (default ``euclidean``).
    target_chunk_tokens : int | None
        Desired chunk *granularity*, decoupled from the ceiling. HDBSCAN
        clusters are uneven, so a dominant theme can fit the ceiling whole and
        become one fat, slow map call. When set, a cluster larger than this is
        split into roughly equal sub-chunks. The effective split budget is
        ``min(target_chunk_tokens, max_total_tokens)``, so the ceiling always
        wins. ``None`` keeps the old behaviour (split only at the ceiling).
    Records within every chunk are always ordered chronologically by their
    ``created`` metadata (undated records last), so a chunk reads as a time
    series and a split cluster yields contiguous time-windows.

    Returns
    -------
    tuple[Chunk, ...]
        Chunks whose records partition the input exactly. Noise points
        are collected into uncategorised chunk(s) with ``label == -1``.

    Raises
    ------
    ValueError
        If ``records`` and ``vectors`` differ in length.
    """
    if len(records) != len(vectors):
        raise ValueError(
            f"records ({len(records)}) and vectors ({len(vectors)}) length mismatch"
        )
    if not records:
        return ()

    # The granularity target never overrides the hard ceiling: the effective
    # split budget is the smaller of the two, so a chunk can't overflow a call
    # no matter how target_chunk_tokens is configured.
    split_budget = max_total_tokens
    if target_chunk_tokens is not None:
        split_budget = min(target_chunk_tokens, max_total_tokens)

    # When the corpus is smaller than min_cluster_size, HDBSCAN cannot form
    # any cluster and would error in some backends. Treat the whole batch as
    # uncategorised noise instead so the coverage invariant still holds.
    if len(records) < min_cluster_size:
        return tuple(
            _budgeted_chunks(
                _sort_by_date(tuple(records)),
                label=-1,
                is_uncategorised=True,
                max_total_tokens=split_budget,
                chars_per_token=chars_per_token,
            )
        )

    matrix = np.asarray(vectors, dtype=np.float64)
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric=metric)
    labels = clusterer.fit_predict(matrix)

    # Group record indices by label.
    by_label: dict[int, list[int]] = {}
    for idx, raw_label in enumerate(labels):
        by_label.setdefault(int(raw_label), []).append(idx)

    chunks: list[Chunk] = []
    for label, indices in sorted(by_label.items()):
        group = _sort_by_date(tuple(records[i] for i in indices))
        chunks.extend(
            _budgeted_chunks(
                group,
                label=label,
                is_uncategorised=(label == -1),
                max_total_tokens=split_budget,
                chars_per_token=chars_per_token,
            )
        )

    # Defence in depth: assert the coverage invariant the tests rely on.
    covered = {r.id for chunk in chunks for r in chunk.records}
    expected = {r.id for r in records}
    if covered != expected:
        raise AssertionError(
            "clustering dropped or duplicated records: "
            f"missing={expected - covered} extra={covered - expected}"
        )

    return tuple(chunks)
