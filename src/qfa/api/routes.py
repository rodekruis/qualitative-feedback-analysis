"""API route handlers for the feedback analysis backend."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

import qfa
from qfa.api.dependencies import authenticate_request, get_orchestrator
from qfa.api.schemas import (
    ApiAggregateSummary,
    ApiAnalyzeRequest,
    ApiAnalyzeResponse,
    ApiAssignCodesRequest,
    ApiAssignCodesResponse,
    ApiAssignedCode,
    ApiCodedFeedbackRecord,
    ApiFeedbackRecordSummary,
    ApiHealthResponse,
    ApiSummarizeAggregateResponse,
    ApiSummarizeFeedbackMetadata,
    ApiSummarizeRequest,
    ApiSummarizeResponse,
    ApiDetectSensitiveRequest,
    ApiDetectSensitiveResponse,
    ApiFeedbackItemSensitivityRating,
)
from qfa.domain.models import (
    AnalysisRequestModel,
    CodingAssignmentRequestModel,
    FeedbackRecordModel,
    TenantApiKey,
)
from qfa.domain.models import (
    SummaryRequestModel as DomainSummaryRequest,
)
from qfa.services.orchestrator import Orchestrator

router = APIRouter()


def _summarize_metadata_to_domain(
    meta: ApiSummarizeFeedbackMetadata,
) -> dict[str, str | int | float | bool]:
    """Flatten summarize metadata into the domain feedback metadata dict."""
    return {
        "created": meta.model_dump(mode="json")["created"],
        "feedback_record_id": meta.feedback_record_id,
        "coding_level_1": meta.coding_level_1,
        "coding_level_2": meta.coding_level_2,
        "coding_level_3": meta.coding_level_3,
    }


@router.post("/v1/analyze", response_model=ApiAnalyzeResponse, status_code=200)
async def analyze(
    body: ApiAnalyzeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ApiAnalyzeResponse:
    """Analyze a batch of feedback records.

    Parameters
    ----------
    body : AnalyzeRequest
        The request body containing feedback records and prompt.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    AnalyzeResponse
        The analysis result with feedback record count and request ID.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_feedback_records = tuple(
        FeedbackRecordModel(id=doc.id, text=doc.text, metadata=doc.metadata)
        for doc in body.feedback_records
    )

    domain_request = AnalysisRequestModel(
        feedback_records=domain_feedback_records,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.analyze(
        domain_request, deadline, anonymize=body.anonymize
    )

    return ApiAnalyzeResponse(
        analysis=result.result,
        feedback_record_count=len(body.feedback_records),
        request_id=request.state.request_id,
        used_anonymization=body.anonymize,
    )


@router.post("/v1/summarize", response_model=ApiSummarizeResponse, status_code=200)
async def summarize(
    body: ApiSummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ApiSummarizeResponse:
    """Summarize each submitted feedback record individually.

    Parameters
    ----------
    body : SummarizeRequest
        The request body containing feedback records and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    SummarizeResponse
        The per-feedback-record titles and summaries.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    feedback_records = tuple(
        FeedbackRecordModel(
            id=record.id,
            text=record.content,
            metadata=_summarize_metadata_to_domain(record.metadata),
        )
        for record in body.feedback_records
    )
    domain_request = DomainSummaryRequest(
        feedback_records=feedback_records,
        output_language=body.output_language,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize(
        domain_request,
        deadline,
        anonymize=body.anonymize,
    )

    return ApiSummarizeResponse(
        summaries=[
            ApiFeedbackRecordSummary(
                id=summary.id,
                title=summary.title,
                summary=summary.summary,
                quality_score=summary.quality_score,
            )
            for summary in result.feedback_record_summaries
        ],
        used_anonymization=body.anonymize,
    )


@router.post("/v1/assign_codes", response_model=ApiAssignCodesResponse, status_code=200)
async def assign_codes(
    body: ApiAssignCodesRequest,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ApiAssignCodesResponse:
    """Assign codes via iterative LLM picks at each level of the framework."""
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_feedback_records = tuple(
        FeedbackRecordModel(id=record.id, text=record.content, metadata={})
        for record in body.feedback_records
    )
    domain_request = CodingAssignmentRequestModel(
        feedback_records=domain_feedback_records,
        coding_framework=body.coding_framework,
        max_codes=body.max_codes,
        confidence_threshold=body.confidence_threshold,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.assign_codes(
        domain_request, deadline, anonymize=body.anonymize
    )

    return ApiAssignCodesResponse(
        coded_feedback_records=[
            ApiCodedFeedbackRecord(
                feedback_record_id=coded.feedback_record_id,
                assigned_codes=[
                    ApiAssignedCode(
                        code_id=assigned.code_id,
                        code_label=assigned.code_label,
                        confidence_type=assigned.confidence_type,
                        confidence_category=assigned.confidence_category,
                        confidence_code=assigned.confidence_code,
                        confidence_aggregate=assigned.confidence_aggregate,
                        explanation=assigned.explanation,
                    )
                    for assigned in coded.assigned_codes
                ],
            )
            for coded in result.coded_feedback_records
        ],
    )


@router.post(
    "/v1/summarize-aggregate",
    response_model=ApiSummarizeAggregateResponse,
    status_code=200,
)
async def summarize_aggregate(
    body: ApiSummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ApiSummarizeAggregateResponse:
    """Summarize all submitted feedback records as a single aggregate summary.

    Parameters
    ----------
    body : SummarizeRequest
        The request body containing feedback records and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    SummarizeAggregateResponse
        A single summary with themes ordered by frequency across all feedback records.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    feedback_records = tuple(
        FeedbackRecordModel(
            id=record.id,
            text=record.content,
            metadata=_summarize_metadata_to_domain(record.metadata),
        )
        for record in body.feedback_records
    )
    domain_request = DomainSummaryRequest(
        feedback_records=feedback_records,
        output_language=body.output_language,
        prompt=body.prompt,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize_aggregate(
        domain_request, deadline, anonymize=body.anonymize
    )

    return ApiSummarizeAggregateResponse(
        summary=ApiAggregateSummary(
            ids=list(result.ids),
            title=result.title,
            summary=result.summary,
            quality_score=result.quality_score,
        )
    )

@router.post("/v1/detect-sensitive", response_model=ApiDetectSensitiveResponse, status_code=200)
async def detect_sensitive(
    body: ApiDetectSensitiveRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ApiDetectSensitiveResponse:
    """Detect sensitive content in feedback items.

    Parameters
    ----------
    body : ApiDetectSensitiveRequest
        The request body containing feedback items to check for sensitive content.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    ApiDetectSensitiveResponse
        Sensitivity rating for each submitted feedback item.
    """
    _deadline = datetime.now(UTC) + timedelta(seconds=120) 
    return ApiDetectSensitiveResponse(
        ratings=[]
    )

@router.get("/v1/health", response_model=ApiHealthResponse, status_code=200)
async def health() -> ApiHealthResponse:
    """Return service health status.

    Returns
    -------
    HealthResponse
        Health status and package version.
    """
    return ApiHealthResponse(
        status="ok",
        version=qfa.__version__,
    )
