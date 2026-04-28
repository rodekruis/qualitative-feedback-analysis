"""API route handlers for the feedback analysis backend."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import qfa
from qfa.api.dependencies import (
    authenticate_request,
    get_orchestrator,
    get_usage_repo,
    require_superuser,
)
from qfa.api.schemas import (
    AggregateSummary,
    AllUsageStatsResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    AssignCodesRequest,
    AssignCodesResponse,
    CodeItem,
    CodeItems,
    DistributionStatsResponse,
    FeedbackItemSummary,
    HealthResponse,
    OperationStatsResponse,
    SummarizeAggregateResponse,
    SummarizeFeedbackMetadata,
    SummarizeRequest,
    SummarizeResponse,
    TokenStatsResponse,
    UsageStatsResponse,
)
from qfa.domain.models import (
    AnalysisRequest,
    CodingAssignmentRequest,
    DistributionStats,
    FeedbackItem,
    TenantApiKey,
    TokenStats,
    UsageStats,
)
from qfa.domain.models import (
    SummaryRequest as DomainSummaryRequest,
)
from qfa.domain.ports import OrchestratorPort, UsageRepositoryPort

router = APIRouter()


def _summarize_metadata_to_domain(
    meta: SummarizeFeedbackMetadata,
) -> dict[str, str | int | float | bool]:
    """Flatten summarize metadata into the domain feedback metadata dict."""
    return {
        "created": meta.model_dump(mode="json")["created"],
        "feedback_item_id": meta.feedback_item_id,
        "coding_level_1": meta.coding_level_1,
        "coding_level_2": meta.coding_level_2,
        "coding_level_3": meta.coding_level_3,
    }


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
        FeedbackItem(id=doc.id, text=doc.text, metadata=doc.metadata)
        for doc in body.documents
    )

    domain_request = AnalysisRequest(
        documents=domain_documents,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.analyze(
        domain_request, deadline, anonymize=not body.deactivate_anonymization
    )

    return AnalyzeResponse(
        analysis=result.result,
        document_count=len(body.documents),
        request_id=request.state.request_id,
        used_anonymization=not body.deactivate_anonymization,
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
        failed_calls=stats.failed_calls,
        total_cost_usd=stats.total_cost_usd,
        call_duration=_to_distribution_response(stats.call_duration),
        input_tokens=_to_token_response(stats.input_tokens),
        output_tokens=_to_token_response(stats.output_tokens),
        by_operation=[
            OperationStatsResponse(
                operation=str(op.operation),
                total_calls=op.total_calls,
                failed_calls=op.failed_calls,
                cost_usd=op.cost_usd,
                input_tokens_total=op.input_tokens_total,
                output_tokens_total=op.output_tokens_total,
            )
            for op in stats.by_operation
        ],
    )


def _zero_usage(tenant_id: str | None) -> UsageStatsResponse:
    return UsageStatsResponse(
        tenant_id=tenant_id,
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStatsResponse(avg=0, min=0, max=0, p5=0, p95=0),
        input_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=TokenStatsResponse(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        by_operation=[],
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
    resp = _zero_usage(tenant.tenant_id) if stats is None else _to_usage_response(stats)
    return resp.model_copy(update={"from_": from_, "to": to})


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
    tenants = [_to_usage_response(s) for s in all_stats if s.tenant_id is not None]
    total_entry = next((s for s in all_stats if s.tenant_id is None), None)
    total = (
        _to_usage_response(total_entry)
        if total_entry is not None
        else _zero_usage(None)
    )
    return AllUsageStatsResponse(
        tenants=tenants,
        total=total,
        from_=from_,
        to=to,
    )


@router.post("/v1/summarize", response_model=SummarizeResponse, status_code=200)
async def summarize(
    body: SummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: OrchestratorPort = Depends(get_orchestrator),
) -> SummarizeResponse:
    """Summarize each submitted feedback item individually.

    Parameters
    ----------
    body : SummarizeRequest
        The request body containing feedback items and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : OrchestratorPort
        The orchestrator service, injected via dependency.

    Returns
    -------
    SummarizeResponse
        The per-feedback-item titles and summaries.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    feedback_items = tuple(
        FeedbackItem(
            id=item.id,
            text=item.content,
            metadata=_summarize_metadata_to_domain(item.metadata),
        )
        for item in body.feedback_items
    )
    domain_request = DomainSummaryRequest(
        feedback_items=feedback_items,
        output_language=body.output_language,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize(
        domain_request,
        deadline,
        anonymize=not body.deactivate_anonymization,
    )

    return SummarizeResponse(
        summaries=[
            FeedbackItemSummary(
                id=item.id,
                title=item.title,
                summary=item.summary,
                quality_score=item.quality_score,
            )
            for item in result.feedback_item_summaries
        ],
        used_anonymization=not body.deactivate_anonymization,
    )


@router.post("/v1/assign_codes", response_model=AssignCodesResponse, status_code=200)
async def assign_codes(
    body: AssignCodesRequest,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: OrchestratorPort = Depends(get_orchestrator),
) -> AssignCodesResponse:
    """Assign codes via iterative LLM picks at each level of the framework."""
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_items = tuple(
        FeedbackItem(id=item.id, text=item.content, metadata={})
        for item in body.feedback_items
    )
    domain_request = CodingAssignmentRequest(
        feedback_items=domain_items,
        coding_framework=body.coding_framework,
        max_codes=body.max_codes,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.assign_codes(domain_request, deadline)

    return AssignCodesResponse(
        coded_feedback_items=[
            CodeItems(
                feedback_item_id=coded.feedback_item_id,
                code_items=[
                    CodeItem(
                        code_id=assigned.code_id,
                        code_label=assigned.code_label,
                    )
                    for assigned in coded.assigned_codes
                ],
            )
            for coded in result.coded_feedback_items
        ],
    )


@router.post(
    "/v1/summarize-aggregate",
    response_model=SummarizeAggregateResponse,
    status_code=200,
)
async def summarize_aggregate(
    body: SummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: OrchestratorPort = Depends(get_orchestrator),
) -> SummarizeAggregateResponse:
    """Summarize all submitted feedback items as a single aggregate summary.

    Parameters
    ----------
    body : SummarizeRequest
        The request body containing feedback items and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : OrchestratorPort
        The orchestrator service, injected via dependency.

    Returns
    -------
    SummarizeAggregateResponse
        A single summary with themes ordered by frequency across all items.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    feedback_items = tuple(
        FeedbackItem(
            id=item.id,
            text=item.content,
            metadata=_summarize_metadata_to_domain(item.metadata),
        )
        for item in body.feedback_items
    )
    domain_request = DomainSummaryRequest(
        feedback_items=feedback_items,
        output_language=body.output_language,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize_aggregate(domain_request, deadline)

    return SummarizeAggregateResponse(
        summary=AggregateSummary(
            ids=list(result.ids),
            title=result.title,
            summary=result.summary,
            quality_score=result.quality_score,
        )
    )


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
