"""API-facing schemas for the usage-tracking endpoints.

Carved out of ``schemas.py`` so that the analyze/summarize/coding
contract isn't crowded by usage-only response shapes. Re-imported by
``routes_usage.py`` only — nothing else depends on these.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_serializer


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


class OperationStatsResponse(BaseModel):
    """Per-operation aggregated stats."""

    operation: str = Field(description="Orchestrator operation name.")
    total_calls: int
    failed_calls: int
    cost_usd: Decimal = Field(
        description="Sum of cost_usd over status='ok' rows in the window.",
    )
    input_tokens_total: int
    output_tokens_total: int

    @field_serializer("cost_usd")
    def _serialize_cost(self, v: Decimal) -> float:
        return float(v)


class UsageStatsResponse(BaseModel):
    """Aggregated usage statistics for a single tenant or grand total.

    Attributes
    ----------
    tenant_id : str | None
        Tenant identifier, or None for grand total.
    from_ : datetime | None
        Echoed inclusive lower bound of the time filter (or None).
    to : datetime | None
        Echoed exclusive upper bound of the time filter (or None).
    total_calls : int
        Total attempts (successful + failed).
    failed_calls : int
        Attempts with ``status='error'``.
    total_cost_usd : Decimal
        Sum of cost over successful attempts only.
    call_duration : DistributionStatsResponse
        Call duration distribution in milliseconds (successful attempts only).
    input_tokens : TokenStatsResponse
        Input token distribution (successful attempts only).
    output_tokens : TokenStatsResponse
        Output token distribution (successful attempts only).
    by_operation : list[OperationStatsResponse]
        Per-operation breakdown, sorted cost desc with ties by operation asc.
    """

    model_config = {"populate_by_name": True}

    tenant_id: str | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    total_calls: int
    failed_calls: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_duration: DistributionStatsResponse
    input_tokens: TokenStatsResponse
    output_tokens: TokenStatsResponse
    by_operation: list[OperationStatsResponse] = Field(default_factory=list)

    @field_serializer("total_cost_usd")
    def _serialize_total_cost(self, v: Decimal) -> float:
        return float(v)


class AllUsageStatsResponse(BaseModel):
    """Per-tenant + grand total usage with optional echoed time window."""

    model_config = {"populate_by_name": True}

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    tenants: list[UsageStatsResponse]
    total: UsageStatsResponse
