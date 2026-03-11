"""API-facing request and response schemas (ADR-007).

These Pydantic models are separate from the domain models so that the
HTTP contract can evolve independently of the core domain.
"""

from pydantic import BaseModel, Field


class DocumentInput(BaseModel):
    """A single document in an analysis request.

    Attributes
    ----------
    id : str
        Unique identifier for the document.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the document.
    """

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    """Request body for the ``POST /v1/analyze`` endpoint.

    Attributes
    ----------
    documents : list[DocumentInput]
        Non-empty list of feedback documents to analyze.
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

    documents: list[DocumentInput] = Field(min_length=1)
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


class DistributionStatsResponse(BaseModel):
    """Distribution statistics for a metric.

    Attributes
    ----------
    avg : float
        Mean value.
    min : float
        Minimum value.
    max : float
        Maximum value.
    p5 : float
        5th percentile.
    p95 : float
        95th percentile.
    """

    avg: float
    min: float
    max: float
    p5: float
    p95: float


class TokenStatsResponse(DistributionStatsResponse):
    """Token distribution statistics with a total count.

    Attributes
    ----------
    total : int
        Total number of tokens.
    """

    total: int


class UsageStatsResponse(BaseModel):
    """Aggregated usage statistics for a single tenant or grand total.

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    total_calls : int
        Total number of LLM calls.
    call_duration : DistributionStatsResponse
        Call duration distribution in milliseconds.
    input_tokens : TokenStatsResponse
        Input token distribution.
    output_tokens : TokenStatsResponse
        Output token distribution.
    """

    tenant_id: str | None = None
    total_calls: int
    call_duration: DistributionStatsResponse
    input_tokens: TokenStatsResponse
    output_tokens: TokenStatsResponse


class AllUsageStatsResponse(BaseModel):
    """Response containing per-tenant and grand total usage statistics.

    Attributes
    ----------
    tenants : list[UsageStatsResponse]
        Per-tenant usage statistics.
    total : UsageStatsResponse
        Grand total across all tenants.
    """

    tenants: list[UsageStatsResponse]
    total: UsageStatsResponse


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
