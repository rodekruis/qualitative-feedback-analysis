"""API route handlers for the feedback analysis backend."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

import qfa
from qfa.api.dependencies import (
    authenticate_request,
    get_orchestrator,
    get_usage_repo,
    require_superuser,
)
from qfa.api.schemas import (
    AllUsageStatsResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    DistributionStatsResponse,
    HealthResponse,
    TokenStatsResponse,
    UsageStatsResponse,
)
from qfa.domain.models import (
    AnalysisRequest,
    DistributionStats,
    FeedbackDocument,
    TenantApiKey,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import OrchestratorPort, UsageRepositoryPort

router = APIRouter()


@router.post("/v1/analyze", response_model=AnalyzeResponse, status_code=200)
async def analyze(
    body: AnalyzeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: OrchestratorPort = Depends(get_orchestrator),
) -> AnalyzeResponse:
    """Analyze a batch of feedback documents.

    Parameters
    ----------
    body : AnalyzeRequest
        The request body containing documents and prompt.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : OrchestratorPort
        The orchestrator service, injected via dependency.

    Returns
    -------
    AnalyzeResponse
        The analysis result with document count and request ID.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_documents = tuple(
        FeedbackDocument(id=doc.id, text=doc.text, metadata=doc.metadata)
        for doc in body.documents
    )

    domain_request = AnalysisRequest(
        documents=domain_documents,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.analyze(domain_request, deadline)

    return AnalyzeResponse(
        analysis=result.result,
        document_count=len(body.documents),
        request_id=request.state.request_id,
    )


def _to_distribution_response(
    stats: DistributionStats | DistributionStatsResponse,
) -> DistributionStatsResponse:
    return DistributionStatsResponse(
        avg=stats.avg,
        min=stats.min,
        max=stats.max,
        p5=stats.p5,
        p95=stats.p95,
    )


def _to_token_response(
    stats: TokenStats | TokenStatsResponse,
) -> TokenStatsResponse:
    return TokenStatsResponse(
        avg=stats.avg,
        min=stats.min,
        max=stats.max,
        p5=stats.p5,
        p95=stats.p95,
        total=stats.total,
    )


def _to_usage_response(stats: UsageStats) -> UsageStatsResponse:
    return UsageStatsResponse(
        tenant_id=stats.tenant_id,
        total_calls=stats.total_calls,
        call_duration=_to_distribution_response(stats.call_duration),
        input_tokens=_to_token_response(stats.input_tokens),
        output_tokens=_to_token_response(stats.output_tokens),
    )


@router.get("/v1/usage", response_model=UsageStatsResponse, status_code=200)
async def usage(
    tenant: TenantApiKey = Depends(authenticate_request),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
) -> UsageStatsResponse:
    """Get usage statistics for the authenticated tenant.

    Parameters
    ----------
    tenant : TenantApiKey
        The authenticated tenant.
    usage_repo : UsageRepositoryPort
        The usage repository.

    Returns
    -------
    UsageStatsResponse
        Aggregated usage statistics for the tenant.
    """
    stats = await usage_repo.get_usage_stats(tenant.tenant_id)
    if stats is None:
        return UsageStatsResponse(
            tenant_id=tenant.tenant_id,
            total_calls=0,
            call_duration=DistributionStatsResponse(avg=0, min=0, max=0, p5=0, p95=0),
            input_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            output_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        )
    return _to_usage_response(stats)


@router.get("/v1/usage/all", response_model=AllUsageStatsResponse, status_code=200)
async def usage_all(
    _tenant: TenantApiKey = Depends(require_superuser),
    usage_repo: UsageRepositoryPort = Depends(get_usage_repo),
) -> AllUsageStatsResponse:
    """Get usage statistics for all tenants. Requires superuser access.

    Parameters
    ----------
    _tenant : TenantApiKey
        The authenticated superuser tenant.
    usage_repo : UsageRepositoryPort
        The usage repository.

    Returns
    -------
    AllUsageStatsResponse
        Per-tenant and grand total usage statistics.
    """
    all_stats = await usage_repo.get_all_usage_stats()

    tenants = [_to_usage_response(s) for s in all_stats if s.tenant_id is not None]
    total_entry = next((s for s in all_stats if s.tenant_id is None), None)

    if total_entry is None:
        total = UsageStatsResponse(
            tenant_id=None,
            total_calls=0,
            call_duration=DistributionStatsResponse(avg=0, min=0, max=0, p5=0, p95=0),
            input_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
            output_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        )
    else:
        total = _to_usage_response(total_entry)

    return AllUsageStatsResponse(tenants=tenants, total=total)


@router.get("/v1/health", response_model=HealthResponse, status_code=200)
async def health() -> HealthResponse:
    """Return service health status.

    Returns
    -------
    HealthResponse
        Health status and package version.
    """
    return HealthResponse(
        status="ok",
        version=qfa.__version__,
    )
