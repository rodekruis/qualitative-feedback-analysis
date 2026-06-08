"""Tests for HDBSCAN clustering + token-budget chunking.

Why: clustering is the chunking strategy for the map step. The
load-bearing guarantees are (1) every input record lands in exactly one
chunk — never dropped, including outliers — and (2) no chunk exceeds the
token budget, so the recursion trigger in the orchestrator is well-defined.
These are pure, deterministic and tested with hand-built vectors (no model).
"""

from qfa.domain.models import FeedbackRecordModel
from qfa.services.clustering import cluster_records


def _record(rec_id: str, text: str = "feedback") -> FeedbackRecordModel:
    return FeedbackRecordModel(id=rec_id, text=text, metadata={})


def test_every_record_lands_in_exactly_one_chunk() -> None:
    """The union of all chunk records equals the input set, with no duplicates.

    Why: the full-coverage invariant — a QFA user expects the whole batch
    considered; silent record loss would corrupt a trend answer.
    """
    records = tuple(_record(f"r{i}") for i in range(6))
    # Two tight groups plus a lone outlier, in 2-D.
    vectors = (
        (0.0, 0.0),
        (0.1, 0.0),
        (0.0, 0.1),
        (9.0, 9.0),
        (9.1, 9.0),
        (50.0, -50.0),  # outlier → noise
    )
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=10_000,
        chars_per_token=4,
    )
    seen_ids = [r.id for chunk in chunks for r in chunk.records]
    assert sorted(seen_ids) == sorted(r.id for r in records)
    assert len(seen_ids) == len(set(seen_ids)), "a record appeared in two chunks"


def test_noise_points_become_uncategorised_chunk() -> None:
    """HDBSCAN noise (label -1) is routed into an uncategorised chunk, not dropped.

    Why: an outlier may be an emerging rumour; the spec forbids dropping it.
    """
    records = tuple(_record(f"r{i}") for i in range(6))
    vectors = (
        (0.0, 0.0),
        (0.1, 0.0),
        (0.0, 0.1),
        (9.0, 9.0),
        (9.1, 9.0),
        (50.0, -50.0),
    )
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=10_000,
        chars_per_token=4,
    )
    uncategorised = [c for c in chunks if c.is_uncategorised]
    assert uncategorised, "outlier was not routed into an uncategorised chunk"
    assert all(c.label == -1 for c in uncategorised)


def test_over_budget_chunk_is_split_into_budget_sized_subchunks() -> None:
    """A cluster exceeding the token budget is split; no resulting chunk overflows.

    Why: this is recursion trigger (1). The orchestrator reduces an
    over-budget cluster's sub-partials first, so chunking must guarantee
    each sub-chunk fits.
    """
    # 10 near-identical records, each ~400 chars → ~100 tokens at cpt=4.
    big_text = "alpha beta gamma delta epsilon zeta " * 11  # ~400 chars
    records = tuple(_record(f"r{i}", text=big_text) for i in range(10))
    vectors = tuple((0.0, float(i) * 0.001) for i in range(10))  # one tight cluster
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=300,  # ~1200 chars budget → forces a split
        chars_per_token=4,
    )
    # Every chunk must fit the budget.
    for chunk in chunks:
        chars = sum(len(r.text) for r in chunk.records)
        assert chars // 4 <= 300, "a chunk exceeds the token budget after splitting"
    # And coverage still holds.
    seen = [r.id for c in chunks for r in c.records]
    assert sorted(seen) == sorted(r.id for r in records)


def test_single_record_corpus_yields_one_chunk() -> None:
    """A one-record corpus produces exactly one chunk covering that record.

    Why: HDBSCAN labels everything noise when there are too few points;
    the function must still cover the record (as uncategorised), not crash.
    """
    records = (_record("only"),)
    chunks = cluster_records(
        records=records,
        vectors=((1.0, 2.0),),
        min_cluster_size=2,
        max_total_tokens=10_000,
        chars_per_token=4,
    )
    seen = [r.id for c in chunks for r in c.records]
    assert seen == ["only"]
