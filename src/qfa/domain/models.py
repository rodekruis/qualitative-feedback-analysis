"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FeedbackDocument(BaseModel):
    """A single feedback document submitted for analysis.

    Attributes
    ----------
    id : str
        Unique identifier for the document.
    text : str
        The feedback text content. Must be between 1 and 100,000 characters.
    metadata : dict[str, str | int | float | bool]
        Optional metadata key-value pairs associated with the document.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AnalysisRequest(BaseModel):
    """A request to analyze one or more feedback documents.

    Attributes
    ----------
    documents : tuple[FeedbackDocument, ...]
        Non-empty tuple of feedback documents to analyze.
    prompt : str
        The analysis prompt. Must be between 1 and 4000 characters.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackDocument, ...] = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=4000)
    tenant_id: str


class AnalysisResult(BaseModel):
    """The result of a feedback analysis.

    Attributes
    ----------
    result : str
        The analysis output text.
    model : str
        The LLM model used for analysis.
    prompt_tokens : int
        Number of tokens in the prompt.
    completion_tokens : int
        Number of tokens in the completion.
    """

    model_config = ConfigDict(frozen=True)

    result: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class LLMResponse(BaseModel):
    """Raw response from an LLM provider.

    Attributes
    ----------
    text : str
        The generated text.
    model : str
        The model that produced the response.
    prompt_tokens : int
        Number of tokens in the prompt.
    completion_tokens : int
        Number of tokens in the completion.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    def __repr__(self) -> str:
        """Representation of the LLMResponse object."""
        return f"LLMResponse(model={self.model}, prompt_tokens={self.prompt_tokens}, completion_tokens={self.completion_tokens}, text='<redacted>')"


class TenantApiKey(BaseModel):
    """An API key associated with a tenant.

    Attributes
    ----------
    name : str
        Human-readable name for the API key.
    key : str
        The API key value.
    tenant_id : str
        The tenant this key belongs to.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    key: str
    tenant_id: str
    is_superuser: bool = False


class LLMCallRecord(BaseModel):
    """A single recorded LLM call for usage tracking.

    Attributes
    ----------
    tenant_id : str
        Tenant that made the call.
    timestamp : datetime
        When the call was made.
    call_duration_ms : int
        Wall-clock duration of the call in milliseconds.
    model : str
        The LLM model used.
    input_tokens : int
        Number of input (prompt) tokens.
    output_tokens : int
        Number of output (completion) tokens.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    timestamp: datetime
    call_duration_ms: int
    model: str
    input_tokens: int
    output_tokens: int


class DistributionStats(BaseModel):
    """Statistical distribution summary.

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

    model_config = ConfigDict(frozen=True)

    avg: float
    min: float
    max: float
    p5: float
    p95: float


class TokenStats(DistributionStats):
    """Token distribution summary with a total count.

    Attributes
    ----------
    total : int
        Total number of tokens.
    """

    total: int


class UsageStats(BaseModel):
    """Aggregated usage statistics for a tenant or grand total.

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    total_calls : int
        Total number of LLM calls.
    call_duration : DistributionStats
        Call duration distribution in milliseconds.
    input_tokens : TokenStats
        Input token distribution.
    output_tokens : TokenStats
        Output token distribution.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    total_calls: int
    call_duration: DistributionStats
    input_tokens: TokenStats
    output_tokens: TokenStats
