"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class FeedbackItem(BaseModel):
    """A single feedback item submitted for analysis."""

    model_config = ConfigDict(frozen=True)

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


class AnalysisRequest(BaseModel):
    """A request to analyze one or more feedback items."""

    model_config = ConfigDict(frozen=True)

    documents: tuple[FeedbackItem, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4000,
        description="Analysis instruction for the model.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AnalysisResult(BaseModel):
    """The result of a feedback analysis."""

    model_config = ConfigDict(frozen=True)

    result: str = Field(description="Analysis output text.")
    model: str = Field(description="LLM model used for analysis.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(description="Number of tokens in the completion.")
    cost: float = Field(description="Estimated analysis cost in USD.")


class SummaryRequest(BaseModel):
    """A request to summarize one or more feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback items to summarize.",
    )
    output_language: str | None = Field(
        default=None,
        description="Optional target language for all summaries.",
    )
    prompt: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional extra instruction appended to the default summarize prompt.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class FeedbackItemSummary(BaseModel):
    """Summary output for a single feedback item."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Identifier of the source feedback item.")
    title: str = Field(description="Generated short title for the feedback item.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback item."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class SummaryResult(BaseModel):
    """The result of summarizing multiple feedback items individually."""

    model_config = ConfigDict(frozen=True)

    feedback_item_summaries: tuple[FeedbackItemSummary, ...] = Field(
        description="Per-feedback-item summaries returned by the summarize flow.",
    )
    cost: float = Field(description="Estimated summarization cost in USD.")


class AggregateSummaryResult(BaseModel):
    """The result of summarizing multiple feedback items as a single aggregate."""

    model_config = ConfigDict(frozen=True)

    ids: tuple[str, ...] = Field(
        description="Identifiers of all source feedback items."
    )
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )
    cost: float = Field(description="Estimated summarization cost in USD.")


class CodingAssignmentRequest(BaseModel):
    """A request to assign hierarchical codes to feedback items.

    Attributes
    ----------
    feedback_items : tuple[FeedbackItem, ...]
        Non-empty tuple of feedback items to code (``text`` is the body to classify).
    coding_framework : dict[str, Any]
        Hierarchical framework payload with top-level ``types`` and nested
        ``categories`` and ``codes``.
    max_codes : int
        Maximum number of leaf codes to retain per feedback item.
    tenant_id : str
        Tenant identifier, injected by the auth layer.
    """

    model_config = ConfigDict(frozen=True)

    feedback_items: tuple[FeedbackItem, ...] = Field(min_length=1)
    coding_framework: dict[str, Any]
    max_codes: int = Field(ge=1, le=50)
    tenant_id: str


class AssignedCode(BaseModel):
    """A single leaf code assigned to a feedback item.

    Attributes
    ----------
    code_id : str
        Stable identifier from the framework (e.g. slug path).
    code_label : str
        Human-readable code name.
    """

    model_config = ConfigDict(frozen=True)

    code_id: str
    code_label: str


class CodedFeedbackItem(BaseModel):
    """Coding output for one feedback item.

    Attributes
    ----------
    feedback_item_id : str
        Identifier of the source feedback item.
    assigned_codes : tuple[AssignedCode, ...]
        Leaf codes selected for this item.
    """

    model_config = ConfigDict(frozen=True)

    feedback_item_id: str
    assigned_codes: tuple[AssignedCode, ...]


class CodingAssignmentResult(BaseModel):
    """The result of assigning codes to multiple feedback items.

    Attributes
    ----------
    coded_feedback_items : tuple[CodedFeedbackItem, ...]
        Per-item coding results, aligned with the request order.
    """

    model_config = ConfigDict(frozen=True)

    coded_feedback_items: tuple[CodedFeedbackItem, ...]


class LLMResponse(BaseModel):
    """Raw response from an LLM provider."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="Generated text.")
    model: str = Field(description="Model that produced the response.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(description="Number of tokens in the completion.")
    cost: float = Field(description="Estimated request cost in USD.")


class TenantApiKey(BaseModel):
    """An API key associated with a tenant."""

    model_config = ConfigDict(frozen=True)

    key_id: str = Field(description="Unique identifier for the API key.")
    name: str = Field(description="Human-readable name for the API key.")
    key: SecretStr = Field(description="Secret API key value.")
    tenant_id: str = Field(description="Tenant identifier this key belongs to.")
    is_superuser: bool = False


class Operation(StrEnum):
    """Orchestrator operations that produce LLM calls.

    Stored as plain strings in the database; new members can be added
    without a DB migration. ``UNKNOWN`` is a sentinel for backfilled rows
    from before per-operation tracking was introduced and must never be
    removed (removal would orphan historical rows).
    """

    ANALYZE = "analyze"
    SUMMARIZE = "summarize"
    SUMMARIZE_AGGREGATE = "summarize_aggregate"
    ASSIGN_CODES = "assign_codes"
    UNKNOWN = "unknown"


class CallStatus(StrEnum):
    """Outcome of a single LLM call attempt."""

    OK = "ok"
    ERROR = "error"


class CallContext(BaseModel):
    """Per-call context propagated via ContextVar from orchestrator to tracker.

    Attributes
    ----------
    tenant_id : str
        Tenant making the call.
    operation : Operation
        Public orchestrator operation that issued the call.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    operation: Operation


class LLMCallRecord(BaseModel):
    """A single recorded LLM call attempt for usage and cost tracking.

    Recorded once per LLM-call attempt — success or failure. ``cost_usd``
    and token counts are populated only for successful attempts; failures
    record zeros plus ``error_class``.

    Attributes
    ----------
    tenant_id : str
        Tenant that made the call.
    operation : Operation
        Public orchestrator operation that issued the call.
    timestamp : datetime
        UTC wall-clock when the call started.
    call_duration_ms : int
        Wall-clock duration of the call in milliseconds.
    model : str
        The LLM model used.
    input_tokens : int
        Number of input (prompt) tokens; 0 on failure.
    output_tokens : int
        Number of output (completion) tokens; 0 on failure.
    cost_usd : Decimal
        Estimated cost in USD; 0 on failure.
    status : CallStatus
        Outcome of the attempt.
    error_class : str | None
        ``type(exc).__name__`` when ``status == CallStatus.ERROR``;
        ``None`` otherwise. Enforced by ``model_validator``.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    operation: Operation
    timestamp: datetime
    call_duration_ms: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    status: CallStatus
    error_class: str | None = None

    @model_validator(mode="after")
    def _error_class_iff_error(self) -> "LLMCallRecord":
        if self.status == CallStatus.ERROR and self.error_class is None:
            raise ValueError("error_class is required when status='error'")
        if self.status == CallStatus.OK and self.error_class is not None:
            raise ValueError("error_class must be None when status='ok'")
        return self


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


class OperationStats(BaseModel):
    """Per-operation aggregated stats for a tenant or grand total.

    Attributes
    ----------
    operation : Operation
        The orchestrator operation.
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    cost_usd : Decimal
        Sum of ``cost_usd`` for successful attempts only.
    input_tokens_total : int
        Sum of ``input_tokens`` for successful attempts only.
    output_tokens_total : int
        Sum of ``output_tokens`` for successful attempts only.
    """

    model_config = ConfigDict(frozen=True)

    operation: Operation
    total_calls: int
    failed_calls: int
    cost_usd: Decimal
    input_tokens_total: int
    output_tokens_total: int


class UsageStats(BaseModel):
    """Aggregated usage statistics for a tenant or grand total.

    The token and duration distributions and ``total_cost_usd`` are scoped
    to ``status='ok'`` rows. ``total_calls`` and ``failed_calls`` count all
    attempts including failures (policy "alpha").

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    total_cost_usd : Decimal
        Sum of cost over successful attempts only.
    call_duration : DistributionStats
        Call duration distribution in milliseconds (successful attempts only).
    input_tokens : TokenStats
        Input token distribution (successful attempts only).
    output_tokens : TokenStats
        Output token distribution (successful attempts only).
    by_operation : tuple[OperationStats, ...]
        Per-operation breakdown, sorted ``cost_usd`` desc with ties broken
        by ``operation`` asc.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    total_calls: int
    failed_calls: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_duration: DistributionStats
    input_tokens: TokenStats
    output_tokens: TokenStats
    by_operation: tuple[OperationStats, ...] = ()
