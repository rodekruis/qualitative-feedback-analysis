"""Domain model for a thematic chunk of feedback records.

Kept in a dedicated module separate from
:mod:`qfa.domain.clustering_models` so that
:class:`~qfa.domain.models.FeedbackRecordModel` can be imported here
without creating a circular dependency: ``models`` imports
``CodingTrendTable`` from ``clustering_models``, which does NOT import
``FeedbackRecordModel``, so the cycle is avoided.
"""

from pydantic import BaseModel, ConfigDict, Field

from qfa.domain.models import FeedbackRecordModel


class Chunk(BaseModel):
    """A thematically coherent group of feedback records for the map step.

    A chunk is produced either by a cluster, by splitting an
    over-budget cluster into sub-chunks, or by collecting HDBSCAN noise
    points into one or more "uncategorised" chunks. Every input record
    belongs to exactly one chunk (the full-coverage invariant).
    """

    model_config = ConfigDict(frozen=True)

    label: int = Field(
        description=(
            "Cluster label. Real clusters use 0..n; the noise/uncategorised"
            " chunk uses -1 (HDBSCAN's noise label)."
        ),
    )
    is_uncategorised: bool = Field(
        description=(
            "True when this chunk holds HDBSCAN noise points (outliers)."
            " Outliers are analysed like any other chunk, never dropped."
        ),
    )
    records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Feedback records in this chunk (non-empty).",
    )
