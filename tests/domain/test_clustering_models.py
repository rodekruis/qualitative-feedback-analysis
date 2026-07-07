"""Tests for the hierarchical-analysis domain models.

Why: these frozen value objects are the contract between clustering,
coding-trends, and the orchestrator. The tests pin field names, the
full-coverage relationship between a chunk set and its records, and
immutability — so a rename in one layer can't silently drift from another.
"""

import pytest
from pydantic import ValidationError

from qfa.domain.chunk_models import Chunk
from qfa.domain.clustering_models import (
    CodingTrendCell,
    CodingTrendTable,
)
from qfa.domain.models import FeedbackRecordMetadataModel, FeedbackRecordModel


def _record(rec_id: str) -> FeedbackRecordModel:
    return FeedbackRecordModel(
        id=rec_id, content="x", metadata=FeedbackRecordMetadataModel()
    )


def test_chunk_is_frozen() -> None:
    """A ``Chunk`` is immutable so it cannot be mutated after clustering.

    Why: chunks flow through map/reduce; accidental mutation would make
    the full-coverage invariant unverifiable.
    """
    chunk = Chunk(
        label=0,
        is_uncategorised=False,
        records=(_record("a"),),
    )
    with pytest.raises(ValidationError):
        chunk.label = 1  # type: ignore[misc]


def test_uncategorised_chunk_carries_noise_label() -> None:
    """The noise/uncategorised chunk uses label ``-1`` and is flagged.

    Why: outliers (HDBSCAN label -1) are never dropped; downstream code
    keys off ``is_uncategorised`` to surface emerging signals.
    """
    chunk = Chunk(label=-1, is_uncategorised=True, records=(_record("a"),))
    assert chunk.label == -1
    assert chunk.is_uncategorised is True


def test_coding_trend_table_round_trips_cells() -> None:
    """``CodingTrendTable`` preserves the (code, period, count) cells it is built from.

    Why: the table is an independent, non-LLM faithfulness anchor fed
    into the reduce prompt; its counts must be exactly reproducible.
    """
    table = CodingTrendTable(
        periods=("2024-01", "2024-02"),
        cells=(
            CodingTrendCell(code="Water", period="2024-01", count=3),
            CodingTrendCell(code="Water", period="2024-02", count=1),
        ),
    )
    assert table.periods == ("2024-01", "2024-02")
    assert table.cells[0].count == 3
