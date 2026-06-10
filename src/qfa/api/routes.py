"""API route handlers for the feedback analysis backend."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

import qfa
from qfa.api.dependencies import (
    authenticate_request,
    call_scope_for,
    get_orchestrator,
)
from qfa.api.schemas import (
    ApiAnalyzeBulkResponse,
    ApiAnalyzeRequest,
    ApiAssignCodesRequest,
    ApiAssignCodesResponse,
    ApiAssignedCode,
    ApiCodingNode,
    ApiCodingTrendCell,
    ApiCodingTrends,
    ApiDetectSensitiveRequest,
    ApiDetectSensitiveResponse,
    ApiHealthResponse,
    ApiSummarizeBulkRequest,
    ApiSummarizeBulkResponse,
    ApiSummarizeRequest,
    ApiSummarizeResponse,
)
from qfa.domain.models import (
    AnalysisRequestModel,
    CodingAssignmentRequestModel,
    CodingFramework,
    CodingNode,
    FeedbackRecordModel,
    SensitivityAnalysisRequestModel,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    TenantApiKey,
)
from qfa.domain.usage_models import CallContext, Operation
from qfa.services.orchestrator import Orchestrator


def _to_domain_coding_node(node: ApiCodingNode) -> CodingNode:
    return CodingNode(
        name=node.name, children=[_to_domain_coding_node(c) for c in node.children]
    )


router = APIRouter()


@router.post(
    "/v1/analyze-bulk",
    response_model=ApiAnalyzeBulkResponse,
    status_code=200,
    tags=["Bulk Inference"],
)
async def analyze_bulk(
    body: ApiAnalyzeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.ANALYZE)),
) -> ApiAnalyzeBulkResponse:
    """Analyze a batch of feedback records for trends and themes.

    The analyst prompt in ``body.prompt`` is wrapped in a structural
    envelope together with the feedback records, and the model is
    instructed to treat record text as data, not instructions.  A
    server-side disclaimer is prepended to every analysis output.

    A second LLM call (AI-as-judge) scores the analysis and produces a
    natural-language ``uncertainty_explanation`` the analyst can use to
    spot unsupported claims.  If the judge call fails, the response still
    returns 200 with ``quality_score=null`` and a constant unavailable
    message in ``uncertainty_explanation``.

    **Modes**:

    - ``single_pass`` (default) — one LLM call within the token cap.
    - ``hierarchical`` — embed → cluster → map → reduce pipeline for
      large corpora (> 5x the single-call token cap). Returns an
      additional ``confidence`` field in the response.

    The deterministic ``coding_trends`` table is populated for **both**
    modes — it is built from input metadata and does not depend on the
    LLM call or chunking. The ``period`` request field controls the
    table's granularity (``day`` / ``week`` / ``month``); omit it to
    use the server-side default.

    **Edge cases**:

    - Input that exceeds the token cap for ``single_pass`` → 413
      ``payload_too_large`` (use ``mode=hierarchical`` for large corpora).
    - Injection-like text in record content or metadata is neutralised
      structurally by the envelope; regex-based detection is a separate
      guard handled by the LLM adapter.

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
        The analysis result with quality score, uncertainty explanation,
        feedback record count, and request ID. ``coding_trends`` is
        populated for both modes whenever metadata permits; ``confidence``
        is populated only for ``hierarchical`` mode.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=600)

    domain_feedback_records = tuple(
        FeedbackRecordModel(id=doc.id, content=doc.content, metadata=doc.metadata)
        for doc in body.feedback_records
    )

    domain_request = AnalysisRequestModel(
        feedback_records=domain_feedback_records,
        prompt=body.prompt,
        output_language=body.output_language,
        tenant_id=tenant.tenant_id,
        mode=body.mode,
        period=body.period,
    )

    if body.mode == "hierarchical":
        result = await orchestrator.analyze_hierarchical(domain_request, deadline)
    else:
        result = await orchestrator.analyze_bulk(domain_request, deadline)

    # Map coding_trends domain model → API schema when present.
    api_coding_trends: ApiCodingTrends | None = None
    if result.coding_trends is not None:
        api_coding_trends = ApiCodingTrends(
            periods=list(result.coding_trends.periods),
            cells=[
                ApiCodingTrendCell(code=c.code, period=c.period, count=c.count)
                for c in result.coding_trends.cells
            ],
        )

    return ApiAnalyzeBulkResponse(
        analysis=result.result,
        quality_score=result.quality_score,
        uncertainty_explanation=result.uncertainty_explanation,
        feedback_record_count=len(body.feedback_records),
        request_id=request.state.request_id,
        confidence=result.confidence,
        coding_trends=api_coding_trends,
    )


@router.post(
    "/v1/summarize-bulk",
    response_model=ApiSummarizeBulkResponse,
    status_code=200,
    tags=["Bulk Inference"],
)
async def summarize_bulk(
    body: ApiSummarizeBulkRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.SUMMARIZE_AGGREGATE)),
) -> ApiSummarizeBulkResponse:
    """Summarize all submitted feedback records as a single aggregate summary.

    Parameters
    ----------
    body : ApiSummarizeBulkRequest
        The request body containing feedback records and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    ApiSummarizeBulkResponse
        A single summary with themes ordered by frequency across all feedback records.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    feedback_records = tuple(
        FeedbackRecordModel(
            id=record.id,
            content=record.content,
            metadata=record.metadata,
        )
        for record in body.feedback_records
    )
    domain_request = SummaryRequestModel(
        feedback_records=feedback_records,
        output_language=body.output_language,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize_bulk(domain_request, deadline)

    return ApiSummarizeBulkResponse(
        ids=list(result.ids),
        title=result.title,
        summary=result.summary,
        quality_score=result.quality_score,
    )


@router.post(
    "/v1/summarize",
    response_model=ApiSummarizeResponse,
    status_code=200,
    tags=["Inference"],
)
async def summarize(
    body: ApiSummarizeRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.SUMMARIZE)),
) -> ApiSummarizeResponse:
    """Summarize submitted feedback record.

    Parameters
    ----------
    body : ApiSummarizeRequest
        The request body containing feedback records and summarization options.
    request : Request
        The incoming HTTP request.
    tenant : TenantApiKey
        The authenticated tenant, injected via dependency.
    orchestrator : Orchestrator
        The orchestrator service, injected via dependency.

    Returns
    -------
    ApiSummarizeResponse
        The per-feedback-record titles and summaries.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_request = SingleSummaryRequestModel(
        feedback_record=FeedbackRecordModel(
            id=body.feedback_record.id,
            content=body.feedback_record.content,
            metadata=body.feedback_record.metadata,
        ),
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.summarize(
        domain_request,
        deadline,
    )

    return ApiSummarizeResponse(
        id=result.id,
        title=result.title,
        summary=result.summary,
        quality_score=result.quality_score,
    )


@router.post(
    "/v1/assign-codes",
    response_model=ApiAssignCodesResponse,
    status_code=200,
    tags=["Inference"],
)
async def assign_codes(
    body: ApiAssignCodesRequest,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.ASSIGN_CODES)),
) -> ApiAssignCodesResponse:
    """Assign codes via iterative LLM picks at each level of the framework."""
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    domain_request = CodingAssignmentRequestModel(
        feedback_record=FeedbackRecordModel(
            id=body.feedback_record.id,
            content=body.feedback_record.content,
            metadata=body.feedback_record.metadata,
        ),
        coding_levels=CodingFramework(
            root_codes=[
                _to_domain_coding_node(n) for n in body.coding_levels.root_codes
            ]
        ),
        max_codes=body.max_codes,
        confidence_threshold=body.confidence_threshold,
        tenant_id=tenant.tenant_id,
    )

    result = await orchestrator.assign_codes(domain_request, deadline)
    coded = result.coded_feedback_records[0]

    return ApiAssignCodesResponse(
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


@router.post(
    "/v1/detect-sensitive",
    response_model=ApiDetectSensitiveResponse,
    status_code=200,
    tags=["Inference"],
)
async def detect_sensitive(
    body: ApiDetectSensitiveRequest,
    request: Request,
    tenant: TenantApiKey = Depends(authenticate_request),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    _scope: CallContext = Depends(call_scope_for(Operation.DETECT_SENSITIVE)),
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
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    result = await orchestrator.detect_sensitive_content(
        SensitivityAnalysisRequestModel(
            feedback_record=FeedbackRecordModel(
                id=body.feedback_record.id,
                content=body.feedback_record.content,
                metadata=body.feedback_record.metadata,
            ),
            tenant_id=tenant.tenant_id,
        ),
        deadline,
    )

    return ApiDetectSensitiveResponse(
        id=result.feedback_record_id,
        is_sensitive=result.is_sensitive,
        explanation=result.explanation,
        sensitivity_types=[st.value for st in result.sensitivity_types],
    )


@router.get(
    "/v1/health", response_model=ApiHealthResponse, status_code=200, tags=["Default"]
)
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
