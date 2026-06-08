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

import hdbscan
import numpy as np

from qfa.domain.chunk_models import Chunk
from qfa.domain.models import FeedbackRecordModel

logger = logging.getLogger(__name__)


def _estimate_tokens(
    records: tuple[FeedbackRecordModel, ...], chars_per_token: int
) -> int:
    """Estimate tokens for a group of records by total text length."""
    return sum(len(r.text) for r in records) // chars_per_token


def _split_to_budget(
    records: tuple[FeedbackRecordModel, ...],
    *,
    max_total_tokens: int,
    chars_per_token: int,
) -> list[tuple[FeedbackRecordModel, ...]]:
    """Greedily pack records into groups that each fit the token budget.

    Records are appended in order; a new group starts whenever adding the
    next record would exceed ``max_total_tokens``. A single record larger
    than the budget still occupies its own group (it cannot be split
    further here — the orchestrator's per-chunk recursion handles it).
    """
    groups: list[tuple[FeedbackRecordModel, ...]] = []
    current: list[FeedbackRecordModel] = []
    current_chars = 0
    budget_chars = max_total_tokens * chars_per_token
    for record in records:
        rec_chars = len(record.text)
        if current and current_chars + rec_chars > budget_chars:
            groups.append(tuple(current))
            current = []
            current_chars = 0
        current.append(record)
        current_chars += rec_chars
    if current:
        groups.append(tuple(current))
    return groups


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
        Per-chunk token budget; over-budget groups are split.
    chars_per_token : int
        Char-to-token conversion ratio for the budget estimate.
    metric : str
        HDBSCAN distance metric (default ``euclidean``).

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

    # When the corpus is smaller than min_cluster_size, HDBSCAN cannot form
    # any cluster and would error in some backends. Treat the whole batch as
    # uncategorised noise instead so the coverage invariant still holds.
    if len(records) < min_cluster_size:
        return tuple(
            _budgeted_chunks(
                tuple(records),
                label=-1,
                is_uncategorised=True,
                max_total_tokens=max_total_tokens,
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
        group = tuple(records[i] for i in indices)
        chunks.extend(
            _budgeted_chunks(
                group,
                label=label,
                is_uncategorised=(label == -1),
                max_total_tokens=max_total_tokens,
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
