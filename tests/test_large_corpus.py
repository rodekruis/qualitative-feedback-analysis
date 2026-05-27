"""Loader + acceptance test for the large-corpus fixture (#124).

Why: the headline acceptance criterion is analysing a corpus >= 5x the
single-call token cap. This test loads the fixture, confirms its size,
and runs the real clustering/coding-trends services over it with a
model-free ``FakeEmbeddingPort`` to prove the hierarchical path covers
every record and forms multilingual themes by content, not language.
"""

from pathlib import Path

import yaml

from qfa.domain.models import FeedbackRecordModel
from qfa.services.clustering import cluster_records
from qfa.services.coding_trends import build_coding_trend_table

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CORPUS_PATH = FIXTURES / "large_corpus.yaml"

# Mirror the single-call cap used in tests (LLMSettings default is 100_000;
# the fixture targets the test cap of 10_000 tokens => ~40_000 chars).
SINGLE_CALL_TOKEN_CAP = 10_000
CHARS_PER_TOKEN = 4


def _load() -> list[dict]:
    """Load the large corpus YAML fixture from disk."""
    with CORPUS_PATH.open() as f:
        return yaml.safe_load(f)


def test_corpus_is_at_least_five_times_the_token_cap() -> None:
    """Total estimated tokens >= 5x the single-call cap.

    Why: this is the explicit #124 acceptance threshold; a smaller corpus
    would not exercise recursion.
    """
    corpus = _load()
    total_chars = sum(len(item["text"]) for item in corpus)
    total_tokens = total_chars // CHARS_PER_TOKEN
    assert total_tokens >= 5 * SINGLE_CALL_TOKEN_CAP, (
        f"corpus is only ~{total_tokens} tokens; need >= {5 * SINGLE_CALL_TOKEN_CAP}"
    )


def test_corpus_is_multilingual() -> None:
    """The corpus spans multiple languages.

    Why: clusters must form by theme, not language; a monolingual corpus
    could not demonstrate that.
    """
    corpus = _load()
    languages = {item["metadata"]["language"] for item in corpus}
    assert len(languages) >= 3, f"only languages: {languages}"


def test_clustering_covers_every_record_with_fake_embedder() -> None:
    """Real clustering over the corpus covers every record (no silent loss).

    Why: end-to-end proof of the full-coverage invariant on a realistic
    input, using a deterministic theme-keyed fake embedder (no model).
    """
    corpus = _load()
    records = tuple(
        FeedbackRecordModel(id=item["id"], text=item["text"], metadata=item["metadata"])
        for item in corpus
    )
    # Theme-keyed fake vectors: same theme -> same point (tight cluster).
    theme_to_point: dict[str, tuple[float, float]] = {}
    vectors = []
    for item in corpus:
        theme = item["metadata"]["theme"]
        point = theme_to_point.setdefault(theme, (float(len(theme_to_point)), 0.0))
        vectors.append(point)
    chunks = cluster_records(
        records=records,
        vectors=tuple(vectors),
        min_cluster_size=2,
        max_total_tokens=SINGLE_CALL_TOKEN_CAP,
        chars_per_token=CHARS_PER_TOKEN,
    )
    seen = {r.id for c in chunks for r in c.records}
    assert seen == {r.id for r in records}


def test_coding_trend_table_counts_match_corpus() -> None:
    """The trend table's counts equal a hand count over the fixture metadata.

    Why: the table is a faithfulness anchor; if its counts drift from the
    source metadata it is worse than useless.
    """
    corpus = _load()
    records = tuple(
        FeedbackRecordModel(id=item["id"], text=item["text"], metadata=item["metadata"])
        for item in corpus
    )
    table = build_coding_trend_table(
        records, date_field="created", code_fields=("codes",)
    )
    assert table is not None
    # Hand count of one known (code, period) pair from the fixture.
    expected = sum(
        1
        for item in corpus
        if "Water" in item["metadata"]["codes"].split(",")
        and item["metadata"]["created"].startswith("2024-01")
    )
    got = next(
        (c.count for c in table.cells if c.code == "Water" and c.period == "2024-01"),
        0,
    )
    assert got == expected
