"""Tests for HDBSCAN clustering + token-budget chunking.

Why: clustering is the chunking strategy for the map step. The
load-bearing guarantees are (1) every input record lands in exactly one
chunk — never dropped, including outliers — and (2) no chunk exceeds the
token budget, so the recursion trigger in the orchestrator is well-defined.
These are pure, deterministic and tested with hand-built vectors (no model).
"""

from qfa.domain.models import FeedbackRecordMetadataModel, FeedbackRecordModel
from qfa.services.clustering import _split_to_budget, cluster_records


def _record(
    rec_id: str, content: str = "feedback", *, created: str | None = None
) -> FeedbackRecordModel:
    return FeedbackRecordModel(
        id=rec_id,
        content=content,
        metadata=FeedbackRecordMetadataModel(
            created=created if created is not None else ""
        ),
    )


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
    records = tuple(_record(f"r{i}", content=big_text) for i in range(10))
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
        chars = sum(len(r.content) for r in chunk.records)
        assert chars // 4 <= 300, "a chunk exceeds the token budget after splitting"
    # And coverage still holds.
    seen = [r.id for c in chunks for r in c.records]
    assert sorted(seen) == sorted(r.id for r in records)


def test_large_cluster_is_split_by_target_below_the_llm_cap() -> None:
    """A cluster far under the LLM cap is still split when over target_chunk_tokens.

    Why: this is the latency fix. HDBSCAN yields one dominant theme that fits
    the 100k LLM ceiling whole, so without a separate granularity target it
    becomes one fat map call that dictates the concurrent wall-clock. The
    target must split it even though the LLM cap would not.
    """
    # 30 short records in one tight cluster: ~10 tokens each (~40 chars),
    # ~300 tokens total — nowhere near the 100k LLM cap, but over a 100-token
    # target, so it must split.
    records = tuple(_record(f"r{i}", content="x" * 40) for i in range(30))
    vectors = tuple((0.0, float(i) * 0.001) for i in range(30))
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=100_000,  # LLM cap would never split this
        chars_per_token=4,
        target_chunk_tokens=100,  # but the target does
    )
    assert len(chunks) > 1, "a target far below the cap did not split the cluster"
    for chunk in chunks:
        tokens = sum(len(r.content) for r in chunk.records) // 4
        assert tokens <= 100, "a sub-chunk exceeded the target size"
    seen = [r.id for c in chunks for r in c.records]
    assert sorted(seen) == sorted(r.id for r in records)


def test_split_to_budget_produces_balanced_not_remainder_groups() -> None:
    """A single over-budget cluster splits into ~equal groups, not fill+remainder.

    Why: this is the tail-flattening guarantee, tested on the splitter directly
    so HDBSCAN's own (intentional) size variance doesn't confound it. Greedy
    fill-to-budget would yield e.g. [6,6,6,4]; balanced yields [6,6,5,5].
    """
    # 22 uniform records of 40 chars (10 tokens) each; budget 60 tokens
    # (240 chars) → ceil(880/240) = 4 groups.
    records = tuple(_record(f"r{i}", content="x" * 40) for i in range(22))
    groups = _split_to_budget(records, max_total_tokens=60, chars_per_token=4)
    counts = sorted(len(g) for g in groups)
    assert counts == [5, 5, 6, 6], f"not balanced into equal contiguous parts: {counts}"
    # Hard budget still respected by every group.
    for group in groups:
        assert sum(len(r.content) for r in group) // 4 <= 60


def test_split_to_budget_grows_part_count_when_a_balanced_part_overflows() -> None:
    """Part count grows past the average when one balanced part busts the budget.

    Why: ``ceil(total / budget)`` is only a lower bound — record-size variance
    can leave a balanced part over budget, and the hard ceiling must still hold.
    The splitter must add parts until every one fits.
    """
    # Three tiny records + one record that alone is ~half the budget. A naive
    # 2-way balanced split would pair the big record with a small one and may
    # fit, so make the big one dominate: budget 30 tokens (120 chars).
    records = (
        _record("big", content="x" * 110),  # ~27 tokens, near the budget alone
        _record("s1", content="x" * 40),
        _record("s2", content="x" * 40),
        _record("s3", content="x" * 40),
    )
    groups = _split_to_budget(records, max_total_tokens=30, chars_per_token=4)
    for group in groups:
        assert sum(len(r.content) for r in group) // 4 <= 30, "a group busts the budget"
    seen = [r.id for g in groups for r in g]
    assert sorted(seen) == ["big", "s1", "s2", "s3"]


def test_target_above_the_llm_cap_still_respects_the_cap() -> None:
    """The hard LLM cap is honoured even when target_chunk_tokens exceeds it.

    Why: target_chunk_tokens is a granularity hint, not an escape hatch. The
    effective split budget is min(target, cap), so a misconfigured large target
    can never produce a chunk that overflows a single LLM call.
    """
    records = tuple(_record(f"r{i}", content="x" * 40) for i in range(20))
    vectors = tuple((0.0, float(i) * 0.001) for i in range(20))
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=50,  # the real ceiling
        chars_per_token=4,
        target_chunk_tokens=1_000_000,  # absurd target must not win
    )
    for chunk in chunks:
        tokens = sum(len(r.content) for r in chunk.records) // 4
        assert tokens <= 50, "a chunk exceeded the hard LLM cap"


def test_records_are_sorted_by_date_within_each_chunk() -> None:
    """Records inside every chunk are ordered by their date metadata.

    Why: the map/reduce steps look for trends, and a chunk presented in
    chronological order lets the model narrate change over time. When a big
    cluster is split, date-ordering also makes each sub-chunk a contiguous
    time window rather than an arbitrary slice.
    """
    dates = ["2024-03-01", "2024-01-01", "2024-02-15", "2024-01-10", "2024-04-01"]
    records = tuple(_record(f"r{i}", created=dates[i]) for i in range(5))
    vectors = tuple((0.0, float(i) * 0.001) for i in range(5))  # one tight cluster
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=100_000,
        chars_per_token=4,
        date_field="created",
    )
    for chunk in chunks:
        seen = [r.metadata.created for r in chunk.records]
        assert seen == sorted(seen), f"chunk not in date order: {seen}"


def test_records_without_a_parseable_date_sort_last_and_stably() -> None:
    """Undated/unparseable records sort after dated ones, preserving input order.

    Why: sorting must be deterministic (the coverage invariant and reproducible
    runs depend on it). Missing or malformed dates get a stable end position
    instead of crashing or shuffling.
    """
    records = (
        _record("a", created="2024-02-01"),
        _record("b"),  # no date
        _record("c", created="2024-01-01"),
        _record("d", created="not-a-date"),  # unparseable
        _record("e"),  # no date
    )
    vectors = tuple((0.0, float(i) * 0.001) for i in range(5))
    chunks = cluster_records(
        records=records,
        vectors=vectors,
        min_cluster_size=2,
        max_total_tokens=100_000,
        chars_per_token=4,
        date_field="created",
    )
    # All records land in one chunk (tight cluster, well under budget).
    assert len(chunks) == 1
    order = [r.id for r in chunks[0].records]
    # Dated records first in date order; undated/unparseable keep input order.
    assert order == ["c", "a", "b", "d", "e"], order


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
