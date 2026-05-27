"""Domain models for the coding-trend table used in hierarchical analysis.

These models are free of any import from :mod:`qfa.domain.models` so that
``models.py`` can safely import :class:`CodingTrendTable` without creating
a circular dependency. The chunk model (:class:`~qfa.domain.chunk_models.Chunk`),
which DOES import :class:`~qfa.domain.models.FeedbackRecordModel`, lives in
:mod:`qfa.domain.chunk_models` instead.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from pydantic import BaseModel, ConfigDict, Field


class CodingTrendCell(BaseModel):
    """One (code, period, count) cell of the coding-trend table."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="Coding label (per the coding framework).")
    period: str = Field(description="Time period bucket, e.g. ``2024-01``.")
    count: int = Field(
        ge=0, description="Number of records coded with this code in this period."
    )


class CodingTrendTable(BaseModel):
    """Deterministic, non-LLM count of codes over time periods.

    Built from feedback-record metadata; fed into the reduce prompt as a
    faithfulness anchor. When metadata is absent the table is omitted
    (``None`` at call sites), and reduce degrades to text-only synthesis.
    """

    model_config = ConfigDict(frozen=True)

    periods: tuple[str, ...] = Field(
        description="Sorted, de-duplicated period buckets covered by the table.",
    )
    cells: tuple[CodingTrendCell, ...] = Field(
        description="Per-(code, period) counts. Empty when no codes were found.",
    )
