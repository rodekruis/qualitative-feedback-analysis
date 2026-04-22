"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class FeedbackItemInput(BaseModel):
    """A single feedback item in an analysis request."""

    id: str = Field(description="Unique identifier for the feedback item.")
    text: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback text content.",
    )
    metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Optional metadata key-value pairs associated with the feedback item.",
    )


class AnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "documents": [
                        {
                            "id": "doc-001",
                            "text": "The water distribution was well organized but we had to wait for three hours.",
                            "metadata": {"region": "Eastern Province", "year": 2024},
                        },
                        {
                            "id": "doc-002",
                            "text": "Medical staff were very professional. Medicine supply was insufficient.",
                            "metadata": {"region": "Northern Province", "year": 2024},
                        },
                    ],
                    "prompt": "Summarize the main themes and sentiment of the feedback.",
                },
            ],
        },
    }

    documents: list[FeedbackItemInput] = Field(
        min_length=1,
        description="Non-empty list of feedback items to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4_000,
        description="Analysis instruction for the model.",
    )


class AnalyzeResponse(BaseModel):
    """Response body for the ``POST /v1/analyze`` endpoint."""

    analysis: str = Field(description="Analysis output text.")
    document_count: int = Field(description="Number of documents that were analyzed.")
    request_id: str = Field(description="Unique identifier for this request.")


class SummarizeFeedbackMetadata(BaseModel):
    """Metadata for a feedback item in a summarize request."""

    created: datetime = Field(
        description="Timestamp when the feedback item was created."
    )
    feedback_item_id: str = Field(description="Source feedback item identifier.")
    coding_level_1: str = Field(description="Level 1 coding label.")
    coding_level_2: str = Field(description="Level 2 coding label.")
    coding_level_3: str = Field(description="Level 3 coding label.")


class SummarizeFeedbackItem(BaseModel):
    """A single feedback item for ``POST /v1/summarize``."""

    id: str = Field(description="Unique identifier for the feedback item.")
    content: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback content to summarize.",
    )
    metadata: SummarizeFeedbackMetadata = Field(
        description="Structured metadata for the feedback item.",
    )


class SummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "feedback_items": [
                        {
                            "id": "doc-001",
                            "content": (
                                "After the storm damaged the main supply line, a water distribution "
                                "point was set up near the schoolyard with ropes and signs so people "
                                "knew where to queue. Volunteers explained the ration clearly - two "
                                "jerrycans per family per day - and the process felt orderly compared "
                                "to the chaos in the first days. The main problem was the waiting time: "
                                "many of us stood in line for more than three hours in the sun, "
                                "including elderly people and parents with small children, and some "
                                "had to leave before reaching the front because of work or caring "
                                "for relatives at home. A few argued that those who arrived earliest "
                                "should not lose out when the team stopped for breaks. People "
                                "appreciated that distribution was organized, but the long wait made "
                                "it hard for everyone to benefit fairly."
                            ),
                            "metadata": {
                                "created": "2024-06-01T12:00:00Z",
                                "feedback_item_id": "fi-001",
                                "coding_level_1": "Water",
                                "coding_level_2": "Distribution",
                                "coding_level_3": "Waiting times",
                            },
                        },
                        {
                            "id": "doc-002",
                            "content": (
                                "During the mobile clinic in the settlement after the floods, the "
                                "medical staff treated people with respect and explained things "
                                "clearly; several of us felt reassured even though we had waited most "
                                "of the morning in the heat. The nurses worked steadily and the "
                                "doctor listened properly before prescribing. What frustrated many "
                                "families was that essential medicines ran out before midday - "
                                "especially antibiotics and chronic medication for older people - so "
                                "some had to leave with prescriptions but no drugs, and others were "
                                "told to come back the next day without any guarantee that stock "
                                "would arrive. A few parents said their children's fever had still "
                                "not been checked by the time the team packed up. Overall the care "
                                "was professional, but unless supplies match the number of people, "
                                "the visit feels incomplete and people lose trust in follow-up."
                            ),
                            "metadata": {
                                "created": "2024-06-02T09:30:00Z",
                                "feedback_item_id": "fi-002",
                                "coding_level_1": "Health",
                                "coding_level_2": "Staff",
                                "coding_level_3": "Supplies",
                            },
                        },
                    ],
                    "output_language": "English",
                    "prompt": "Focus on operational issues and beneficiary experience.",
                },
            ],
        },
    }

    feedback_items: list[SummarizeFeedbackItem] = Field(
        min_length=1,
        description="Non-empty list of feedback items to summarize individually.",
    )
    output_language: str | None = Field(
        default=None,
        description="Optional target language for summaries and titles for every item.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=4_000,
        description="Optional extra instruction appended to the default summarize prompt.",
    )


class FeedbackItemSummary(BaseModel):
    """Summary response item for a single feedback item."""

    id: str = Field(description="Identifier of the source feedback item.")
    title: str = Field(description="Generated short title for the feedback item.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback item."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge score for summary quality in the range 0.0-1.0.",
    )


class SummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint."""

    summaries: list[FeedbackItemSummary] = Field(
        description="Title and summary for each submitted feedback item.",
    )


class CodingNode(BaseModel):
    """Contains the node of a singular coding and its' children."""

    name: str = Field(description="Name of this coding")
    children: list["CodingNode"] = Field(
        default_factory=list,
        description="Child coding nodes nested under this coding.",
    )

    @property
    def has_children(self) -> bool:
        """If this coding node has children nodes, or is a leaf."""
        return len(self.children) > 0

    def max_child_depth(self) -> int:
        """Returns the distance to the furthest child."""
        if not self.has_children:
            return 0
        return max([child.max_child_depth() for child in self.children]) + 1

    def min_child_depth(self) -> int:
        """Returns the distance to the furthest child."""
        if not self.has_children:
            return 0
        return min([child.min_child_depth() for child in self.children]) + 1


class CodingLevels(BaseModel):
    """Contains the hierarchical codings used for classification."""

    root_codes: list[CodingNode] = Field(
        description="The root (level 1) codes of your classification.", min_length=1
    )

    @model_validator(mode="after")
    def verify_all_codes_have_same_depth(self) -> "CodingLevels":
        """Checks if all codes have the same depth."""
        max_lengths = set(code.max_child_depth() for code in self.root_codes)
        min_lengths = set(code.min_child_depth() for code in self.root_codes)
        if len(max_lengths.union(min_lengths)) > 1:
            raise ValueError(
                f"All codes must have the same depth {min_lengths=} {max_lengths=}"
            )

        return self


class HealthResponse(BaseModel):
    """Response body for the ``GET /v1/health`` endpoint."""

    status: str = Field(description="Service health status.")
    version: str = Field(description="Package version string.")


class ErrorFieldDetail(BaseModel):
    """Per-field validation error detail."""

    field: str = Field(description="Field that failed validation.")
    issue: str = Field(description="Description of the validation issue.")


class ErrorDetail(BaseModel):
    """Structured error information."""

    code: str = Field(description="Stable string error code.")
    message: str = Field(description="Human-readable error message.")
    request_id: str = Field(
        description="Unique identifier of the request that caused the error.",
    )
    fields: list[ErrorFieldDetail] | None = Field(
        default=None,
        description="Per-field validation details, present only for 422 responses.",
    )


class ErrorResponse(BaseModel):
    """Envelope for all error responses."""

    error: ErrorDetail = Field(description="Error detail payload.")
