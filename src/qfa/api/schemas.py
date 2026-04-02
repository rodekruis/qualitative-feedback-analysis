"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

from pydantic import BaseModel, Field


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


class SummarizeRequest(BaseModel):
    """Request body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    feedback_items : list[FeedbackItemInput]
        Non-empty list of feedback items to summarize individually.
    output_language : str | None
        Optional target language for the summaries.
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
                            "text": "The water distribution was well organized but we had to wait for three hours.",
                            "metadata": {"region": "Eastern Province", "year": 2024},
                        },
                        {
                            "id": "doc-002",
                            "text": "Medical staff were very professional. Medicine supply was insufficient.",
                            "metadata": {"region": "Northern Province", "year": 2024},
                        },
                    ],
                    "output_language": "English",
                    "prompt": "Focus on operational issues and beneficiary experience.",
                },
            ],
        },
    }

    feedback_items: list[FeedbackItemInput] = Field(min_length=1)
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
    """

    id: str
    title: str
    summary: str


class SummarizeResponse(BaseModel):
    """Response body for the ``POST /v1/summarize`` endpoint.

    Attributes
    ----------
    feedback_item_summaries : list[FeedbackItemSummary]
        Per-feedback-item summaries returned by the service.
    request_id : str
        Unique identifier for this request.
    """

    feedback_item_summaries: list[FeedbackItemSummary]
    request_id: str


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
