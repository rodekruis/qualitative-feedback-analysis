"""API route handlers for the feedback analysis backend."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

import qfa
from qfa.api.dependencies import (
    authenticate_request,
    get_orchestrator,
)
from qfa.api.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    DocumentSummary,
    HealthResponse,
    SummarizeRequest,
    SummarizeResponse,
)
from qfa.domain.models import (
    AnalysisRequest,
    FeedbackDocument,
    TenantApiKey,
)
from qfa.domain.models import (
    SummaryRequest as DomainSummaryRequest,
)
from qfa.domain.ports import OrchestratorPort

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


@router.post("/v1/summarize", response_model=SummarizeResponse, status_code=200)
async def summarize(
    body: SummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: OrchestratorPort = Depends(get_orchestrator),
) -> SummarizeResponse:
    """Summarize each submitted document individually.

    Parameters
    ----------
    body : SummarizeRequest
        The request body containing documents and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : OrchestratorPort
        The orchestrator service, injected via dependency.

    Returns
    -------
    SummarizeResponse
        The per-document summaries with request ID.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_documents = tuple(
        FeedbackDocument(id=doc.id, text=doc.text, metadata=doc.metadata)
        for doc in body.documents
    )
    domain_request = DomainSummaryRequest(
        documents=domain_documents,
        output_language=body.output_language,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize(domain_request, deadline)

    return SummarizeResponse(
        summaries=[
            DocumentSummary(id=item.id, title=item.title, summary=item.summary)
            for item in result.summaries
        ],
        request_id=request.state.request_id,
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
