"""API route handlers for the usage-tracking endpoints.

Owns its own ``APIRouter``; mounted by ``create_app`` alongside the
main router. Carved out of ``routes.py`` so the analyze/summarize/coding
flow isn't interleaved with usage-stat marshalling.
"""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query

from qfa.api.dependencies import (
    authenticate_request,
    get_usage_repo,
    require_superuser,
)
from qfa.api.schemas_usage import (
    AllUsageByOperationResponse,
    AllUsageStatsResponse,
    UsageStatsResponse,
)
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import UsageRepositoryPort
from qfa.domain.usage_models import (
    DistributionStats,
    OperationUsageStats,
    TenantUsageStats,
    UsageMetrics,
)

router = APIRouter()

_FROM_DESCRIPTION = (
    "Inclusive lower bound for the query window. ISO-8601 timestamp with "
    "explicit timezone (e.g. `2026-04-01T00:00:00Z`); naive datetimes are "
    "rejected with 422. Omit to start at the beginning of recorded history. "
    "Together with `to`, defines a half-open `[from, to)` window so "
    "consecutive windows can be chained without double-counting boundary rows."
)

_TO_DESCRIPTION = (
    "Exclusive upper bound for the query window. ISO-8601 timestamp with "
    "explicit timezone (e.g. `2026-05-01T00:00:00Z`); naive datetimes are "
    "rejected with 422. Must be strictly greater than `from` when both are "
    "supplied. Omit to extend up to the current time."
)

_TIME_FILTER_EXAMPLES = ["2026-04-01T00:00:00Z", "2026-04-15T12:30:00+02:00"]


def _zero_usage_metrics() -> UsageMetrics:
    """Build a zero ``UsageMetrics`` (no calls in window) used as a fallback."""
    return UsageMetrics(
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
    )


def _zero_usage_stats(tenant_id: str | None) -> TenantUsageStats:
    """Build a domain ``TenantUsageStats`` representing an empty time window.

    Used as the fallback grand-total in ``/v1/usage/all/by-tenant`` when no
    rows matched the time filter. Populates both the per-invocation
    (inherited) fields and the ``llm_call_stats`` block with zeros, and an
    empty ``operations`` tuple — matching the wire shape clients see in
    any other empty-window case.
    """
    zero = _zero_usage_metrics()
    return TenantUsageStats(
        tenant_id=tenant_id,
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        llm_call_stats=zero,
        operations=(),
    )


def _zero_operation_usage_stats() -> OperationUsageStats:
    """Build an ``OperationUsageStats`` representing an empty grand total.

    Used as the fallback grand-total entry in ``/v1/usage/all/by-operation``
    when no rows matched the time filter. ``operation`` is ``None`` (the
    grand-total sentinel); ``tenants`` is empty.
    """
    zero = _zero_usage_metrics()
    return OperationUsageStats(
        operation=None,
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        llm_call_stats=zero,
        tenants=(),
    )


def _parse_time_window(
    from_: datetime | None, to: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Validate and normalise the ``from``/``to`` query window.

    Both values must be timezone-aware; ``to`` must be strictly greater
    than ``from``.
    """
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_error",
                    "message": f"{name!r} must be timezone-aware",
                },
            )
    if from_ is not None and to is not None and to <= from_:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "'to' must be strictly greater than 'from'",
            },
        )
    if from_ is not None:
        from_ = from_.astimezone(UTC)
    if to is not None:
        to = to.astimezone(UTC)
    return from_, to


@router.get(
    "/v1/usage",
    response_model=UsageStatsResponse,
    status_code=200,
    tags=["Usage Tracking"],
)
async def usage(
    tenant: TenantApiKey = Depends(authenticate_request),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(
        default=None,
        alias="from",
        description=_FROM_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
    to: datetime | None = Query(
        default=None,
        description=_TO_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
) -> UsageStatsResponse:
    """Usage statistics for the authenticated tenant within an optional window.

    The response carries two views of the same data:

    - **Per-invocation** (inherited top-level fields): each distinct
      ``call_id`` counts as one. Multi-LLM-call operations (e.g.
      ``/v1/assign_codes``) collapse to a single entry. ``call_duration``
      sums the LLM-call durations within one invocation — equal to
      wall-clock latency for sequential invocations, **overestimating**
      wall-clock when the orchestrator fans out LLM calls in parallel
      via ``asyncio.gather``.
    - **Per-LLM-call** (``llm_call_stats``): each LLM call attempt counts
      as one. Identical semantics to the pre-#91 behaviour. Use this
      when you want today's "every row counts as one call" view.

    ``operations`` carries a per-operation breakdown of the same data.
    Each entry has the same shape (per-invocation top-level +
    ``llm_call_stats``). The list is sorted by ``total_cost_usd``
    descending with ties broken by ``operation`` ascending; operations
    with zero calls in the window are omitted.

    **`failed_calls` semantics (per-invocation top-level):** an invocation
    counts as failed only when *every* LLM call within its ``call_id``
    has ``status='error'``. Mixed-status invocations do NOT count.
    Failed-only invocations are excluded from the per-invocation
    distributions (so failures cannot skew latency/token quantiles) but
    their cost is still summed into ``total_cost_usd`` — the grand total
    reflects what was actually spent, including invocations the provider
    billed before erroring. Every individual error row is still counted
    in ``llm_call_stats.failed_calls``.

    **Backwards-compatible numerics:** ``total_cost_usd``,
    ``input_tokens.total``, and ``output_tokens.total`` are unchanged
    vs. the pre-#91 implementation. **Numerics that have changed
    semantics** for multi-LLM-call operations: ``total_calls``,
    ``failed_calls``, and every ``avg/min/max/p5/p95`` field. Clients
    needing the previous semantics should read ``llm_call_stats``.

    Parameters
    ----------
    tenant : TenantApiKey
        The authenticated tenant.
    usage_repo : UsageRepositoryPort
        The usage repository.
    from_ : datetime | None
        Inclusive lower bound (UTC tz-aware), or None.
    to : datetime | None
        Exclusive upper bound (UTC tz-aware), or None.

    Returns
    -------
    UsageStatsResponse
        Aggregated usage statistics for the tenant in the time window.
    """
    from_, to = _parse_time_window(from_, to)
    stats = await usage_repo.get_usage_stats(tenant.tenant_id, from_=from_, to=to)
    return UsageStatsResponse(
        **stats.model_dump(),
        from_=from_,  # type: ignore[ty:unknown-argument]  # ty does note support Pydantic fields with an alias
        to=to,
    )


@router.get(
    "/v1/usage/all/by-tenant",
    response_model=AllUsageStatsResponse,
    status_code=200,
    tags=["Usage Tracking"],
)
async def usage_all_by_tenant(
    _tenant: TenantApiKey = Depends(require_superuser),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(
        default=None,
        alias="from",
        description=_FROM_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
    to: datetime | None = Query(
        default=None,
        description=_TO_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
) -> AllUsageStatsResponse:
    """Per-tenant and grand-total usage statistics. Requires superuser access.

    Response shape: ``tenants`` is a list of per-tenant ``TenantUsageStats``
    (sorted alphabetically by ``tenant_id``); ``total`` is the
    cross-tenant grand total (``tenant_id`` is null). Every entry —
    per-tenant and grand-total — carries the same dual-view shape as
    ``GET /v1/usage``: per-invocation top-level fields, an
    ``llm_call_stats`` block with the per-LLM-call view, and an
    ``operations`` tuple sorted by cost desc (ties: operation asc,
    empties omitted).

    Tenants with zero calls in the window are filtered from
    ``tenants``. The ``total`` entry is always present (zero-filled when
    the window is empty).

    See ``GET /v1/usage`` for the full per-field semantic contract,
    including the per-invocation ``failed_calls`` rule and the
    backwards-compatibility note on which numerics changed. For the
    inverse hierarchy (operations top-level, tenants nested), see
    ``GET /v1/usage/all/by-operation``.

    Parameters
    ----------
    _tenant : TenantApiKey
        The authenticated superuser tenant.
    usage_repo : UsageRepositoryPort
        The usage repository.
    from_ : datetime | None
        Inclusive lower bound (UTC tz-aware), or None.
    to : datetime | None
        Exclusive upper bound (UTC tz-aware), or None.

    Returns
    -------
    AllUsageStatsResponse
        Per-tenant and grand total usage statistics within the window.
    """
    from_, to = _parse_time_window(from_, to)
    all_stats = await usage_repo.get_all_usage_by_tenant(from_=from_, to=to)
    tenants = [s for s in all_stats if s.tenant_id is not None]
    total = next(
        (s for s in all_stats if s.tenant_id is None),
        _zero_usage_stats(None),
    )
    return AllUsageStatsResponse(
        tenants=tenants,
        total=total,
        from_=from_,  # type: ignore[ty:unknown-argument]  # ty does note support Pydantic fields with an alias
        to=to,
    )


@router.get(
    "/v1/usage/all/by-operation",
    response_model=AllUsageByOperationResponse,
    status_code=200,
    tags=["Usage Tracking"],
)
async def usage_all_by_operation(
    _tenant: TenantApiKey = Depends(require_superuser),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(
        default=None,
        alias="from",
        description=_FROM_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
    to: datetime | None = Query(
        default=None,
        description=_TO_DESCRIPTION,
        examples=_TIME_FILTER_EXAMPLES,
    ),
) -> AllUsageByOperationResponse:
    """Per-operation and grand-total usage statistics. Requires superuser access.

    Inverse hierarchy of ``GET /v1/usage/all/by-tenant``: top-level
    aggregation is by orchestrator operation, with a nested ``tenants``
    breakdown under each operation. Useful for answering "where is the
    spend going, regardless of tenant" and "which tenants drive each
    operation".

    Response shape: ``operations`` is a list of per-operation
    ``OperationUsageStats`` (sorted by ``total_cost_usd`` desc, ties
    broken by ``operation`` asc); ``total`` is the cross-operation grand
    total (``operation`` is null). Every entry — per-operation and
    grand-total — carries per-invocation top-level fields, an
    ``llm_call_stats`` block with the per-LLM-call view, and a
    ``tenants`` tuple sorted by cost desc (ties: tenant_id asc, empties
    omitted).

    Operations with zero calls in the window are filtered from
    ``operations``. The ``total`` entry is always present (zero-filled
    when the window is empty).

    See ``GET /v1/usage`` for the full per-field semantic contract,
    including the per-invocation ``failed_calls`` rule and the
    backwards-compatibility note on which numerics changed.

    Parameters
    ----------
    _tenant : TenantApiKey
        The authenticated superuser tenant.
    usage_repo : UsageRepositoryPort
        The usage repository.
    from_ : datetime | None
        Inclusive lower bound (UTC tz-aware), or None.
    to : datetime | None
        Exclusive upper bound (UTC tz-aware), or None.

    Returns
    -------
    AllUsageByOperationResponse
        Per-operation and grand total usage statistics within the window.
    """
    from_, to = _parse_time_window(from_, to)
    all_stats = await usage_repo.get_all_usage_by_operation(from_=from_, to=to)
    operations = [s for s in all_stats if s.operation is not None]
    total = next(
        (s for s in all_stats if s.operation is None),
        _zero_operation_usage_stats(),
    )
    return AllUsageByOperationResponse(
        operations=operations,
        total=total,
        from_=from_,  # type: ignore[ty:unknown-argument]  # ty does note support Pydantic fields with an alias
        to=to,
    )
