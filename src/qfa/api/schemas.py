"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class FeedbackItemInput(BaseModel):
    """A single feedback item in an analysis request.

    Attributes
    ----------
    id : str
        Unique identifier for the feedback item.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the feedback item.
    """

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint.

    Attributes
    ----------
    documents : list[FeedbackItemInput]
        Non-empty list of feedback items to analyze.
    prompt : str
        The analysis prompt. Must be between 1 and 4,000 characters.
    """

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

    documents: list[FeedbackItemInput] = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4_000)


class AnalyzeResponse(BaseModel):
    """Response body for the ``POST /v1/analyze`` endpoint.

    Attributes
    ----------
    analysis : str
        The analysis output text.
    document_count : int
        Number of documents that were analyzed.
    request_id : str
        Unique identifier for this request.
    """

    analysis: str
    document_count: int
    request_id: str


class SummarizeFeedbackMetadata(BaseModel):
    """Metadata for a feedback item in a summarize request."""

    created: datetime
    feedback_item_id: str
    coding_level_1: str
    coding_level_2: str
    coding_level_3: str


class SummarizeFeedbackItem(BaseModel):
    """A single feedback item for ``POST /v1/summarize``."""

    id: str
    content: str = Field(min_length=1, max_length=100_000)
    metadata: SummarizeFeedbackMetadata


class SummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    feedback_items : list[SummarizeFeedbackItem]
        Non-empty list of feedback items to summarize individually.
    output_language : str | None
        Optional target language for summaries and titles for every item.
    prompt : str | None
        Optional extra instruction appended to the default summarize prompt.
    """

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

    feedback_items: list[SummarizeFeedbackItem] = Field(min_length=1)
    output_language: str | None = None
    prompt: str | None = Field(default=None, max_length=4_000)


class FeedbackItemSummary(BaseModel):
    """Summary response item for a single feedback item.

    Attributes
    ----------
    id : str
        Identifier of the source feedback item.
    title : str
        Generated short title for the feedback item.
    summary : str
        Generated bullet-point summary for the feedback item.
    quality_score : float
        Judge score for the summary text only, in the range 0.0-1.0.
    """

    id: str
    title: str
    summary: str
    quality_score: float = Field(ge=0.0, le=1.0)


class SummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    summaries : list[FeedbackItemSummary]
        Title and summary for each submitted feedback item.
    """

    summaries: list[FeedbackItemSummary]


class CodingNode(BaseModel):
    """Contains the node of a singular coding and its' children."""

    name: str = Field(description="Name of this coding")
    children: list["CodingNode"] = Field(default_factory=list)

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
    """Response body for the ``GET /v1/health`` endpoint.

    Attributes
    ----------
    status : str
        Service health status.
    version : str
        Package version string.
    """

    status: str
    version: str


class ErrorFieldDetail(BaseModel):
    """Per-field validation error detail.

    Attributes
    ----------
    field : str
        The field that failed validation.
    issue : str
        Description of the validation issue.
    """

    field: str
    issue: str


class ErrorDetail(BaseModel):
    """Structured error information.

    Attributes
    ----------
    code : str
        Stable string error code.
    message : str
        Human-readable error message.
    request_id : str
        Unique identifier of the request that caused the error.
    fields : list[ErrorFieldDetail] | None
        Per-field validation details, present only for 422 responses.
    """

    code: str
    message: str
    request_id: str
    fields: list[ErrorFieldDetail] | None = None


class ErrorResponse(BaseModel):
    """Envelope for all error responses.

    Attributes
    ----------
    error : ErrorDetail
        The error detail payload.
    """

    error: ErrorDetail
