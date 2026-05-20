"""Domain models for the feedback analysis backend.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, TypeVar, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    model_validator,
)


class FeedbackRecordModel(BaseModel):
    """A single feedback record submitted for analysis."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Unique identifier for the feedback record.")
    text: str = Field(
        min_length=1,
        max_length=100_000,
        description="Feedback text content.",
    )
    metadata: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Optional metadata key-value pairs associated with the feedback record.",
    )


class AnalysisRequestModel(BaseModel):
    """A request to analyze one or more feedback records."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to analyze.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=4000,
        description="Analysis instruction for the model.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AnalysisResultModel(BaseModel):
    """The result of a feedback analysis."""

    model_config = ConfigDict(frozen=True)

    result: str = Field(description="Analysis output text.")


class SummaryRequestModel(BaseModel):
    """A request to summarize one or more feedback records individually."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to summarize.",
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


class FeedbackRecordSummaryModel(BaseModel):
    """Summary output for a single feedback record."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Identifier of the source feedback record.")
    title: str = Field(description="Generated short title for the feedback record.")
    summary: str = Field(
        description="Generated bullet-point summary for the feedback record."
    )
    quality_score: float = Field(  # TODO implement actual llm-as-a-judge for this field
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class SummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback records individually."""

    model_config = ConfigDict(frozen=True)

    feedback_record_summaries: tuple[FeedbackRecordSummaryModel, ...] = Field(
        description="Per-feedback-record summaries returned by the summarize flow.",
    )


class AggregateSummaryResultModel(BaseModel):
    """The result of summarizing multiple feedback records as a single aggregate.

    # TODO come up with nice solution for non-mutable quality-score, so this can be a frozen class.
    """

    ids: tuple[str, ...] = Field(
        description="Identifiers of all source feedback records."
    )
    title: str = Field(description="Generated short title for the aggregate summary.")
    summary: str = Field(
        description="Generated bullet-point summary ordered by theme frequency."
    )
    quality_score: float = Field(
        description="Judge model score for summary quality in the range 0.0-1.0.",
    )


class CodingAssignmentRequestModel(BaseModel):
    """A request to assign hierarchical codes to feedback records."""

    model_config = ConfigDict(frozen=True)

    feedback_records: tuple[FeedbackRecordModel, ...] = Field(
        min_length=1,
        description="Non-empty tuple of feedback records to code.",
    )
    coding_framework: dict[str, Any] = Field(
        description="Hierarchical coding framework with types, categories, and codes.",
    )
    max_codes: int = Field(
        ge=1,
        le=50,
        description="Maximum number of leaf codes to retain per feedback record.",
    )
    confidence_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum confidence required at each hierarchy level to retain an assignment.",
    )
    tenant_id: str = Field(description="Tenant identifier injected by the auth layer.")


class AssignedCodeModel(BaseModel):
    """A single leaf code assigned to a feedback record."""

    model_config = ConfigDict(frozen=True)

    code_id: str = Field(description="Stable identifier from the coding framework.")
    code_label: str = Field(description="Human-readable code name.")
    confidence_type: float = Field(
        description="Judge confidence that the Type level fits the feedback record (0-1)."
    )
    confidence_category: float = Field(
        description="Judge confidence that the Category level fits the feedback record (0-1)."
    )
    confidence_code: float = Field(
        description="Judge confidence that the Code level fits the feedback record (0-1)."
    )
    confidence_aggregate: float = Field(
        description="Overall confidence, computed as min of the three level confidences."
    )
    explanation: str = Field(
        description="Judge explanation combining scores from all three hierarchy levels."
    )


class CodedFeedbackRecordModel(BaseModel):
    """Coding output for one feedback record."""

    model_config = ConfigDict(frozen=True)

    feedback_record_id: str = Field(
        description="Identifier of the source feedback record.",
    )
    assigned_codes: tuple[AssignedCodeModel, ...] = Field(
        description="Leaf codes selected for this feedback record.",
    )


class CodingAssignmentResultModel(BaseModel):
    """The result of assigning codes to multiple feedback records."""

    model_config = ConfigDict(frozen=True)

    coded_feedback_records: tuple[CodedFeedbackRecordModel, ...] = Field(
        description="Per-feedback-record coding results aligned with the request order.",
    )


# Define a TypeVar that must be a Pydantic BaseModel
T_Response = TypeVar("T_Response", bound=Union[BaseModel, str])


class LLMResponse(BaseModel, Generic[T_Response]):
    """Raw response from an LLM provider."""

    model_config = ConfigDict(frozen=True)

    structured: T_Response = Field(
        description="Parsed response conforming to the expected schema, either a string or Pydantic model.",
    )
    model: str = Field(description="LLM model that produced the response.")
    prompt_tokens: int = Field(description="Number of tokens in the prompt.")
    completion_tokens: int = Field(
        description="Number of tokens in the completion.",
    )
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
    call_id : UUID
        Correlation ID for the API call. All LLM calls made inside one
        ``call_scope`` share this ID, enabling per-invocation cost
        aggregation across the fan-out of LLM calls.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    operation: Operation
    call_id: UUID


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
    call_id : UUID
        Correlation ID linking all LLM calls made within a single API
        invocation. Shared across the fan-out of LLM calls from one
        ``call_scope``, enabling per-invocation aggregation in usage reports.
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
    call_id: UUID
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


class UsageMetrics(BaseModel):
    """Aggregated stats over a set of records.

    Whether the records are per-LLM-call rows or per-invocation roll-ups is
    fixed by the containing field, not by this class. ``UsageMetrics`` is
    used directly for the per-LLM-call ``llm_call_stats`` block on
    ``UsageStats`` and ``OperationStats``, and as the base class for the
    per-invocation totals on ``UsageStats`` / ``OperationStats``.

    Attributes
    ----------
    total_calls : int
        Count of records in scope (LLM-call attempts for ``llm_call_stats``;
        distinct ``call_id`` invocations for the per-invocation view).
    failed_calls : int
        Count of failed records (per-LLM-call: ``status='error'`` rows;
        per-invocation: invocations where *every* row is ``status='error'``).
    total_cost_usd : Decimal
        Sum of ``cost_usd`` over OK records (numerically identical between
        the two views — failed rows already store 0).
    call_duration : DistributionStats
        Distribution of durations in milliseconds. Per-invocation view
        sums ``call_duration_ms`` across the rows of one ``call_id``
        (failed attempts contribute their real duration). Distribution
        excludes all-failed invocations.
    input_tokens : TokenStats
        Distribution + total of input tokens. ``total`` is identical
        across both views; distribution scope differs.
    output_tokens : TokenStats
        Distribution + total of output tokens. Same parity as ``input_tokens``.
    """

    model_config = ConfigDict(frozen=True)

    total_calls: int = Field(
        description=(
            "Count of records in scope: LLM-call attempts on ``llm_call_stats``; "
            "distinct ``call_id`` invocations on the outer (per-invocation) view."
        ),
    )
    failed_calls: int = Field(
        default=0,
        description=(
            "Count of failed records. On ``llm_call_stats`` this is the count "
            "of rows with ``status='error'``. On the outer view it is the count "
            "of invocations where *every* row in the ``call_id`` is ``status='error'`` "
            "— mixed-status invocations do NOT count as failed."
        ),
    )
    total_cost_usd: Decimal = Field(
        default=Decimal("0"),
        description="Sum of ``cost_usd``. Identical across both views.",
    )
    call_duration: DistributionStats = Field(
        description=(
            "Duration distribution in milliseconds. Per-LLM-call view: "
            "individual LLM call latency. Per-invocation view: SUM of "
            "``call_duration_ms`` across all rows of one ``call_id`` (i.e. "
            "total LLM time consumed by the invocation; overestimates "
            "wall-clock for ``asyncio.gather`` fan-outs)."
        ),
    )
    input_tokens: TokenStats = Field(
        description="Input token distribution + total.",
    )
    output_tokens: TokenStats = Field(
        description="Output token distribution + total.",
    )

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)


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
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    total_calls: int
    failed_calls: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_duration: DistributionStats
    input_tokens: TokenStats
    output_tokens: TokenStats

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)
