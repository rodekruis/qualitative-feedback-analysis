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
from qfa.api.schemas_usage import AllUsageStatsResponse, UsageStatsResponse
from qfa.domain.models import (
    DistributionStats,
    TenantApiKey,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import UsageRepositoryPort

router = APIRouter()


def _zero_usage_stats(tenant_id: str | None) -> UsageStats:
    """Build a domain ``UsageStats`` representing an empty time window."""
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0),
        input_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
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


@router.get("/v1/usage", response_model=UsageStatsResponse, status_code=200)
async def usage(
    tenant: TenantApiKey = Depends(authenticate_request),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
) -> UsageStatsResponse:
    """Usage statistics for the authenticated tenant within an optional window.

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
    if stats is None:
        stats = _zero_usage_stats(tenant.tenant_id)
    return UsageStatsResponse(**stats.model_dump(), from_=from_, to=to)


@router.get("/v1/usage/all", response_model=AllUsageStatsResponse, status_code=200)
async def usage_all(
    _tenant: TenantApiKey = Depends(require_superuser),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
) -> AllUsageStatsResponse:
    """Per-tenant and grand-total usage statistics. Requires superuser access.

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
    all_stats = await usage_repo.get_all_usage_stats(from_=from_, to=to)
    tenants = [s for s in all_stats if s.tenant_id is not None]
    total = next(
        (s for s in all_stats if s.tenant_id is None),
        _zero_usage_stats(None),
    )
    return AllUsageStatsResponse(
        tenants=tenants,
        total=total,
        from_=from_,
        to=to,
    )
