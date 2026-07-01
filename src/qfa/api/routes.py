"""API route handlers for the feedback analysis backend."""

import logging
from collections.abc import Sequence
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
    ApiFeedbackRecordInput,
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
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    SensitivityAnalysisRequestModel,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    TenantApiKey,
)
from qfa.domain.usage_models import CallContext, Operation
from qfa.services.orchestrator import Orchestrator
from qfa.services.prompts import ANALYZE_DISCLAIMER

logger = logging.getLogger(__name__)

# Explanation returned by the empty-result short-circuits (analyze /
# detect-sensitive) when a request contains no non-empty feedback content and
# is therefore not sent to the LLM.
_NO_CONTENT_EXPLANATION = "No feedback content was provided."


def _to_domain_coding_node(node: ApiCodingNode) -> CodingNode:
    return CodingNode(
        id=node.id,
        name=node.name,
        children=[_to_domain_coding_node(c) for c in node.children],
    )


def _drop_empty_records(
    records: Sequence[ApiFeedbackRecordInput],
) -> list[ApiFeedbackRecordInput]:
    """Return only records with non-empty ``content``, logging any dropped.

    EspoCRM may submit feedback records with a blank description. Such
    records carry no information for the LLM and would violate the domain
    ``FeedbackRecordModel`` invariant (``content`` has ``min_length=1``).
    They are dropped here, at the driving adapter, so the domain core only
    ever sees valid records (issue #138).

    For ``analyze-bulk`` this also forfeits the dropped record's metadata,
    which would otherwise feed the deterministic ``coding_trends`` table
    (codes/dates, built independently of the LLM). In practice that loss is
    negligible: a record with a blank description almost never carries
    coding/date metadata either, so there is no real trend signal to keep —
    which is why we drop in analyze too rather than threading empty records
    through just for their (almost always absent) codes.

    Dropping never misaligns results: bulk endpoints return a single
    aggregate output and single-record endpoints echo the source ``id``,
    so EspoCRM matches responses by id, never by position.
    """
    kept = [record for record in records if record.content]
    dropped = len(records) - len(kept)
    if dropped:
        logger.info(
            "Dropped %d feedback record(s) with empty content before processing.",
            dropped,
        )
    return kept


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
    - Records with empty ``content`` are dropped before analysis (a blank
      EspoCRM description must not fail the whole batch — issue #138).
      ``feedback_record_count`` reflects the records actually analyzed. If
      *every* record is empty the response is a 200 with
      ``feedback_record_count=0`` and a disclaimer-only ``analysis``.
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

    records = _drop_empty_records(body.feedback_records)
    if not records:
        # All records were empty: nothing to analyze. Return a 200 empty
        # result (disclaimer preserved) rather than failing the request.
        return ApiAnalyzeBulkResponse(
            analysis=ANALYZE_DISCLAIMER,
            quality_score=None,
            uncertainty_explanation=_NO_CONTENT_EXPLANATION,
            feedback_record_count=0,
            request_id=request.state.request_id,
            confidence=None,
            coding_trends=None,
        )

    domain_feedback_records = tuple(
        FeedbackRecordModel(
            id=doc.id,
            content=doc.content,
            metadata=FeedbackRecordMetadataModel.model_validate(doc.metadata),
        )
        for doc in records
    )

    domain_request = AnalysisRequestModel(
        feedback_records=domain_feedback_records,
        output_language=body.output_language,
        prompt=body.prompt,
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
        feedback_record_count=len(records),
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

    Records with empty ``content`` are dropped before summarization (a blank
    EspoCRM description must not fail the whole batch — issue #138); their ids
    do not appear in the response ``ids``. If *every* record is empty the
    response is a 200 empty aggregate (``ids=[]``, blank ``title``/``summary``,
    ``quality_score=0.0``).

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

    records = _drop_empty_records(body.feedback_records)
    if not records:
        # All records were empty: nothing to summarize. Return a 200 empty
        # aggregate rather than failing the request.
        return ApiSummarizeBulkResponse(
            ids=[],
            title="",
            summary="",
            quality_score=0.0,
        )

    feedback_records = tuple(
        FeedbackRecordModel(
            id=record.id,
            content=record.content,
            metadata=FeedbackRecordMetadataModel.model_validate(record.metadata),
        )
        for record in records
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
        output_language=body.output_language,
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

    If the record's ``content`` is empty the response is a 200 empty summary
    that still echoes the source ``id`` (blank ``title``/``summary``,
    ``quality_score=0.0``), returned without an LLM call — a blank EspoCRM
    description must not produce a silent 422 (issue #138).

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

    if not body.feedback_record.content:
        # Nothing to summarize: return a 200 empty summary that still
        # carries the source id, without calling the LLM (issue #138).
        return ApiSummarizeResponse(
            id=body.feedback_record.id,
            title="",
            summary="",
            quality_score=0.0,
        )

    domain_request = SingleSummaryRequestModel(
        feedback_record=FeedbackRecordModel(
            id=body.feedback_record.id,
            content=body.feedback_record.content,
            metadata=FeedbackRecordMetadataModel.model_validate(
                body.feedback_record.metadata
            ),
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
    """Assign codes via iterative LLM picks at each level of the framework.

    If the record's ``content`` is empty the response is a 200 with an empty
    ``assigned_codes`` list, returned without an LLM call (issue #138).
    """
    deadline = datetime.now(UTC) + timedelta(seconds=120)

    if not body.feedback_record.content:
        # No content to code: return a 200 empty assignment without an LLM
        # call (issue #138).
        return ApiAssignCodesResponse(assigned_codes=[])

    domain_request = CodingAssignmentRequestModel(
        feedback_record=FeedbackRecordModel(
            id=body.feedback_record.id,
            content=body.feedback_record.content,
            metadata=FeedbackRecordMetadataModel.model_validate(
                body.feedback_record.metadata
            ),
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
                coding_level_1_id=assigned.coding_level_1_id,
                coding_level_1_name=assigned.coding_level_1_name,
                coding_level_2_id=assigned.coding_level_2_id,
                coding_level_2_name=assigned.coding_level_2_name,
                coding_level_3_id=assigned.coding_level_3_id,
                coding_level_3_name=assigned.coding_level_3_name,
                confidence_level_1=assigned.confidence_level_1,
                confidence_level_2=assigned.confidence_level_2,
                confidence_level_3=assigned.confidence_level_3,
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

    If the record's ``content`` is empty the response is a 200 reporting
    ``is_sensitive=False`` with no ``sensitivity_types``, returned without an
    LLM call (issue #138).

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

    if not body.feedback_record.content:
        # No content to evaluate: report not-sensitive without an LLM call
        # (issue #138).
        return ApiDetectSensitiveResponse(
            id=body.feedback_record.id,
            is_sensitive=False,
            explanation=_NO_CONTENT_EXPLANATION,
            sensitivity_types=[],
        )

    result = await orchestrator.detect_sensitive_content(
        SensitivityAnalysisRequestModel(
            feedback_record=FeedbackRecordModel(
                id=body.feedback_record.id,
                content=body.feedback_record.content,
                metadata=FeedbackRecordMetadataModel.model_validate(
                    body.feedback_record.metadata
                ),
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
