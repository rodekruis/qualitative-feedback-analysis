"""Usage tracking + aggregation domain models.

Split out from :mod:`qfa.domain.models` to keep the broad request/response
models separate from the usage-tracking cluster. The cluster has two halves:

- **Persistence primitives** — :class:`Operation`, :class:`CallStatus`,
  :class:`CallContext`, :class:`LLMCallRecord`. These describe a single
  LLM-call attempt and the context propagated to the tracking adapter.
- **Aggregations** — :class:`DistributionStats`, :class:`UsageMetrics`,
  :class:`OperationStats`, :class:`TenantUsageStats`, :class:`TenantStats`,
  :class:`OperationUsageStats`. These are the per-tenant / per-operation
  views returned by ``/v1/usage`` endpoints.

All models are immutable (frozen) Pydantic models per ADR-001.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)


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
    DETECT_SENSITIVE = "detect_sensitive"
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
    """Statistical distribution summary over a numeric column.

    Used uniformly for ``call_duration`` (milliseconds), ``input_tokens``,
    and ``output_tokens``. ``total`` is the sum of the underlying values in
    the window and is identical between the per-invocation and per-LLM-call
    views — both sum the same raw rows, just regrouped.

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
    total : int
        Sum of the values in the window (total milliseconds of LLM time
        for ``call_duration``; total tokens for ``input_tokens`` /
        ``output_tokens``).
    """

    model_config = ConfigDict(frozen=True)

    avg: float
    min: float
    max: float
    p5: float
    p95: float
    total: int


class UsageMetrics(BaseModel):
    """Aggregated stats over a set of records.

    Whether the records are per-LLM-call rows or per-invocation roll-ups is
    fixed by the containing field, not by this class. ``UsageMetrics`` is
    used directly for the per-LLM-call ``llm_call_stats`` block on
    ``TenantUsageStats`` and ``OperationStats``, and as the base class for the
    per-invocation totals on ``TenantUsageStats`` / ``OperationStats``.

    Per-field semantics are in the ``Field(description=...)`` below and
    surface in the OpenAPI schema at ``GET /docs``.
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
        description=(
            "Sum of ``cost_usd`` across every row in scope — including "
            "failed attempts that incurred a real cost. Identical between "
            "the per-invocation view and ``llm_call_stats`` (the same rows "
            "are summed, just regrouped)."
        ),
    )
    call_duration: DistributionStats = Field(
        description=(
            "Duration distribution in milliseconds. Per-LLM-call view: "
            "individual LLM call latency. Per-invocation view: SUM of "
            "``call_duration_ms`` across all rows of one ``call_id`` (i.e. "
            "total LLM time consumed by the invocation; overestimates "
            "wall-clock for ``asyncio.gather`` fan-outs). ``total`` is the "
            "total LLM-time consumed in the window (identical between views)."
        ),
    )
    input_tokens: DistributionStats = Field(
        description="Input token distribution including ``total``.",
    )
    output_tokens: DistributionStats = Field(
        description="Output token distribution including ``total``.",
    )

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)


class OperationStats(UsageMetrics):
    """Per-operation usage stats nested inside a tenant block.

    Inherits all metric fields from :class:`UsageMetrics` (per-invocation
    semantics) and adds the ``operation`` discriminator plus a parallel
    ``llm_call_stats`` block giving the per-LLM-call view for the same
    operation.
    """

    model_config = ConfigDict(frozen=True)

    operation: Operation = Field(
        description="Orchestrator operation this block aggregates.",
    )
    llm_call_stats: UsageMetrics = Field(
        description=(
            "Per-LLM-call view of the same window/operation. ``total_calls`` "
            "here is raw LLM call attempts (not invocations); ``call_duration`` "
            "is individual provider latency. Use ``llm_call_stats.total_calls "
            "/ total_calls`` to compute the fan-out factor for this operation."
        ),
    )


class TenantUsageStats(UsageMetrics):
    """Per-tenant (or grand-total) usage stats — per-invocation + per-LLM-call.

    Inherits per-invocation metric fields from :class:`UsageMetrics` and
    adds the per-LLM-call view, the per-operation breakdown, and the
    optional ``tenant_id`` (``None`` is the grand-total sentinel used by
    ``/v1/usage/all/by-tenant``).
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = Field(
        default=None,
        description="Tenant identifier; ``None`` for the grand-total entry.",
    )
    llm_call_stats: UsageMetrics = Field(
        description=(
            "Per-LLM-call view of the same window/tenant. ``total_calls`` here is "
            "raw LLM-call attempts (not invocations); use the outer ``total_calls`` "
            "for the per-invocation count. Numerics: ``total_cost_usd``, "
            "``input_tokens.total``, ``output_tokens.total`` are identical to the "
            "outer view; counts and distributions differ for multi-LLM-call operations."
        ),
    )
    operations: tuple[OperationStats, ...] = Field(
        default=(),
        description=(
            "Per-operation breakdown, sorted by ``total_cost_usd`` descending with "
            "ties broken by ``operation`` ascending. Operations with zero calls in "
            "the window are omitted."
        ),
    )


class TenantStats(UsageMetrics):
    """Per-tenant usage stats nested inside an operation block.

    Mirrors :class:`OperationStats` but for the inverse hierarchy used by
    ``/v1/usage/all/by-operation``: each ``OperationUsageStats`` carries a
    list of these blocks, one per tenant that has activity for that
    operation in the window. Inherits per-invocation metric fields from
    :class:`UsageMetrics` and adds the ``tenant_id`` discriminator plus the
    parallel ``llm_call_stats`` block for the per-LLM-call view of the same
    (operation, tenant) slice.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(
        description="Tenant identifier this block aggregates.",
    )
    llm_call_stats: UsageMetrics = Field(
        description=(
            "Per-LLM-call view of the same (operation, tenant) slice. "
            "``total_calls`` here is raw LLM-call attempts; use the outer "
            "``total_calls`` for the per-invocation count."
        ),
    )


class OperationUsageStats(UsageMetrics):
    """Per-operation (or grand-total) usage stats with nested per-tenant breakdown.

    Inverse hierarchy of :class:`TenantUsageStats`: top-level aggregation is by
    orchestrator operation, with a list of per-tenant blocks underneath.
    Used by ``/v1/usage/all/by-operation``. ``operation`` is ``None`` on the
    grand-total entry (cross-operation, cross-tenant).
    """

    model_config = ConfigDict(frozen=True)

    operation: Operation | None = Field(
        default=None,
        description="Orchestrator operation; ``None`` for the grand-total entry.",
    )
    llm_call_stats: UsageMetrics = Field(
        description=(
            "Per-LLM-call view of the same window/operation. ``total_calls`` here "
            "is raw LLM-call attempts (not invocations); use the outer "
            "``total_calls`` for the per-invocation count."
        ),
    )
    tenants: tuple[TenantStats, ...] = Field(
        default=(),
        description=(
            "Per-tenant breakdown for this operation, sorted by ``total_cost_usd`` "
            "descending with ties broken by ``tenant_id`` ascending. Tenants with "
            "zero calls for this operation in the window are omitted."
        ),
    )
