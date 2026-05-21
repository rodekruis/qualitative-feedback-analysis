"""Orchestrator service — core business logic for feedback analysis.

Assembles prompts, enforces token limits, filters prompt injection,
manages retries with exponential backoff, and enforces deadlines.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qfa.domain.errors import (
    AnalysisError,
    AnalysisTimeoutError,
    FeedbackTooLargeError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    AnalysisResultModel,
    AssignedCodeModel,
    CodedFeedbackRecordModel,
    CodingAssignmentRequestModel,
    CodingAssignmentResultModel,
    FeedbackRecordModel,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.domain.sensitivity_types import SENSITIVITY_TYPE_DESCRIPTIONS
from qfa.services.coding_classifier import (
    JudgeResponse,
    build_judge_messages,
    build_pick_messages,
    parse_selected_indices,
)
from qfa.services.prompts import (
    ANALYZE_ACTION_PROMPT,
    ANALYZE_DISCLAIMER,
    ANALYZE_GUARDRAILS_PROMPT,
    ANALYZE_SYSTEM_PROMPT,
    JUDGE_UNAVAILABLE_EXPLANATION,
    build_analyze_judge_system_message,
    build_analyze_user_message,
)
from qfa.settings import OrchestratorSettings

logger = logging.getLogger(__name__)

_SENSITIVITY_TYPE_GUIDANCE = "\n".join(
    f"- {sensitivity_type.value}: {description}"
    for sensitivity_type, description in SENSITIVITY_TYPE_DESCRIPTIONS.items()
)

_DEFAULT_SUMMARIZATION_PROMPT = (
    "Summarize the feedback item as concise bullet points.\n"
    "Strict Constraint: The summary must be extremely concise, using no more than 3-5 brief bullet points.\n"
    "Constraint: Each bullet point should be a single sentence fragment focusing only on the core sentiment or issue.\n"
    "Also create a short, 3-5 word descriptive title.\n"
    "Do not include markdown code fences.\n"
    "Use the same language as the input feedback item unless a target language is specified."
)

_DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT = (
    "You are an analytical assistant for a humanitarian organisation (Red Cross).\n"
    "You are given multiple feedback records from community members collected during humanitarian operations.\n"
    "Identify the key themes and issues raised across the feedback records.\n"
    "Order the bullet points from most to least frequently mentioned, so the most important problems are shown first.\n"
    "Each bullet point should name the theme and describe it as a concise sentence fragment.\n"
    "Scale the number of bullet points to the size and diversity of the input — use judgement.\n"
    "Also create a short, 3-5 word descriptive title reflecting the dominant theme.\n"
    "Do not include markdown code fences.\n"
    "Use the same language as the input feedback records unless a target language is specified."
)

_DEFAULT_SENSITIVITY_DETECTION_PROMPT = (
    "Analyze each feedback record and detect whether it contains sensitive content.\n"
    "Classify sensitivity using only the SensitivityType enum values from the response schema.\n"
    "For each record, include a concise natural-language explanation for the classification.\n"
    f"SensitivityType guidance:\n{_SENSITIVITY_TYPE_GUIDANCE}\n"
    "Return one result per input record with the matching feedback_record_id.\n"
    "If no sensitive content is present, return an empty sensitivity_types tuple for that record.\n"
    "Do not include markdown code fences.\n"
    "Note that anonymization might have taken place (e.g. ``<PERSON_0>``, ``<LOCATION_1>``). \n"
    "Please act as if these were not anonymized. For example, if you see ``<PERSON_0>``"
    " treat it as if it said 'John Doe' and classify sensitivity accordingly. \n"
    "Please note that we prefer false positives over false negatives in this classification."
)

_JUDGE_PROMPT = """
You are evaluating the quality of a summary.

Source text:
---
{source_text}
---

Summary:
---
{summary}
---

Score the summary using three criteria. Each must be a float between 0 and 1.

Faithfulness:
1.0 = fully supported by source, no hallucinations
0.5 = mostly correct, minor issues
0.0 = major inaccuracies

Coverage:
1.0 = includes all key points
0.5 = partially covers key points
0.0 = misses most important points

Clarity:
1.0 = very clear and concise
0.5 = somewhat clear
0.0 = confusing or poorly written

Compute the final score as:
quality_score = 0.6 * faithfulness + 0.3 * coverage + 0.1 * clarity

Output rules:
- Return ONLY the final quality_score
- Return a single float between 0 and 1
- No JSON
- No explanation
- No extra text
- Example output: 0.82
"""

#: Minimum time (seconds) required for an LLM attempt to be viable.
_MINIMUM_ATTEMPT_WINDOW = 10.0

_JUDGE_USER_MESSAGE = "."

_AlignedItemT = TypeVar("_AlignedItemT")


class AnalyzeJudgeResult(BaseModel):
    """Structured output of the analyse-judge LLM call.

    The judge returns both a quality score in [0,1] and a short
    natural-language ``uncertainty_explanation`` the analyst can read to
    understand why the score is what it is.
    """

    model_config = ConfigDict(frozen=True)

    quality_score: float = Field(ge=0.0, le=1.0)
    uncertainty_explanation: str = Field(min_length=1)


def _parse_judge_quality_score(raw: str) -> float:
    """Parse a single float on the first line of the judge model output."""
    line = raw.strip().split("\n", maxsplit=1)[0].strip()
    try:
        score = float(line)
    except ValueError as exc:
        raise AnalysisError("LLM judge returned invalid quality score") from exc
    if not 0.0 <= score <= 1.0:
        raise AnalysisError("LLM judge returned quality score outside 0.0-1.0")
    return score


def _build_judge_system_message(source_text: str, summary: str) -> str:
    """Fill the judge prompt with the provided source text and summary."""
    return _JUDGE_PROMPT.format(source_text=source_text, summary=summary)


@dataclass
class _ScoredCode:
    code_id: str
    code_label: str
    confidence_type: float
    confidence_category: float
    confidence_code: float
    explanation_type: str
    explanation_category: str
    explanation_code: str

    @property
    def confidence_aggregate(self) -> float:
        return min(self.confidence_type, self.confidence_category, self.confidence_code)

    @property
    def explanation(self) -> str:
        return (
            f"Type ({self.confidence_type:.2f}): {self.explanation_type} "
            f"Category ({self.confidence_category:.2f}): {self.explanation_category} "
            f"Code ({self.confidence_code:.2f}): {self.explanation_code}"
        )


class Orchestrator:
    """Core orchestration service for feedback analysis.

    Assembles prompts from feedback records, validates input,
    calls the LLM through the ``LLMPort``, and manages retries
    with exponential backoff and deadline enforcement.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Configuration for the orchestrator behaviour.
    llm_timeout_seconds : float
        Maximum time in seconds for a single LLM call.
    max_total_tokens : int
        Maximum estimated total tokens for a single request.
    """

    # Entity types whose placeholders are NOT restored in `analyze` output.
    # Defense in depth for the "do not identify individuals" guardrail in
    # `ANALYZE_GUARDRAILS_PROMPT`: even if the analyse LLM echoes a
    # placeholder we supplied, the analyst never sees the underlying name.
    # Scoped to `analyze` only — `summarize`/`assign_codes` still restore
    # all placeholders because their per-record output is meant to be
    # faithful to the source.
    _ANALYZE_RETAINED_PLACEHOLDER_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"PERSON"}
    )

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        llm_timeout_seconds: float,
        max_total_tokens: int,
    ) -> None:
        self._llm = llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._settings = settings
        self._llm_timeout_seconds = llm_timeout_seconds
        self._max_total_tokens = max_total_tokens

    @classmethod
    def _is_retained_analyze_placeholder(cls, placeholder: str) -> bool:
        """Return True when ``placeholder`` belongs to a retained entity type.

        Placeholders use the form ``<ENTITY_TYPE_N>`` (e.g. ``<PERSON_0>``,
        ``<LOCATION_3>``), so a prefix match on ``<TYPE_`` correctly handles
        any index Presidio chooses.
        """
        return any(
            placeholder.startswith(f"<{entity_type}_")
            for entity_type in cls._ANALYZE_RETAINED_PLACEHOLDER_TYPES
        )

    async def analyze(
        self,
        request: AnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AnalysisResultModel:
        """Analyze a batch of feedback records.

        Two LLM calls are issued: the analysis itself, then a judge call
        that produces ``quality_score`` and ``uncertainty_explanation``.

        Edge cases
        ----------
        - ``mode`` other than ``"single_pass"`` → 422.
        - Judge call failure → 200 with ``quality_score=null`` and the
          constant unavailable-judge explanation.
        - Estimated tokens above the cap → 413 ``payload_too_large``;
          reduce the batch size. Hierarchical / map-reduce is tracked in #124.
        - Existing regex prompt-injection tripwire still applies and
          returns 422 ``prompt_injection_detected``.
        """
        system_message = (
            f"{ANALYZE_SYSTEM_PROMPT}\n\n"
            f"{ANALYZE_GUARDRAILS_PROMPT}\n\n"
            f"{ANALYZE_ACTION_PROMPT}"
        )
        user_message = build_analyze_user_message(
            request.prompt, request.feedback_records
        )

        anonymized_user_message = user_message
        anonymization_mapping: dict[str, str] = {}
        if anonymize:
            anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
                user_message
            )

        analyse_timeout = self._check_deadline_and_get_timeout(deadline)
        analyse_response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=analyse_timeout,
        )
        analysis_text: str = analyse_response.structured

        if anonymize:
            restorable_mapping = {
                placeholder: original
                for placeholder, original in anonymization_mapping.items()
                if not self._is_retained_analyze_placeholder(placeholder)
            }
            analysis_text = self._anonymizer.deanonymize(
                analysis_text, restorable_mapping
            )

        quality_score: float | None
        uncertainty_explanation: str
        try:
            judge_timeout = self._check_deadline_and_get_timeout(deadline)
            judge_system = build_analyze_judge_system_message(
                source_text=anonymized_user_message,
                analyst_prompt=request.prompt,
                analysis=analyse_response.structured,
            )
            judge_response = await self._llm.complete(
                system_message=judge_system,
                user_message=_JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=AnalyzeJudgeResult,
                timeout=judge_timeout,
            )
            quality_score = judge_response.structured.quality_score
            uncertainty_explanation = judge_response.structured.uncertainty_explanation
        except (
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            ValidationError,
            AnalysisError,
        ) as exc:
            logger.warning(
                "Analyse judge call failed: error_class=%s",
                type(exc).__name__,
            )
            quality_score = None
            uncertainty_explanation = JUDGE_UNAVAILABLE_EXPLANATION

        return AnalysisResultModel(
            result=f"{ANALYZE_DISCLAIMER}{analysis_text}",
            quality_score=quality_score,
            uncertainty_explanation=uncertainty_explanation,
        )

    async def summarize(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SummaryResultModel:
        """Summarize each submitted feedback record individually.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback records and options.
        deadline : datetime
            Absolute UTC deadline by which summarization must complete.

        Returns
        -------
        SummaryResult
            The per-feedback-record summaries and titles.

        Raises
        ------
        AnalysisError
            When the LLM returns invalid output or another non-recoverable
            error occurs.
        """
        timeout = self._check_deadline_and_get_timeout(deadline)
        system_message = _DEFAULT_SUMMARIZATION_PROMPT
        if request.output_language:
            system_message += (
                f"\nWrite the title and summary in {request.output_language}."
            )
        if request.prompt:
            system_message += f"\nAdditional instructions: {request.prompt}"

        user_message = str(request.feedback_records)
        anonymized_user_message = user_message
        if anonymize:
            anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
                user_message
            )

        llm_completion = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SummaryResultModel,
            timeout=timeout,
        )

        if anonymize:
            return_model_as_string = llm_completion.structured.model_dump_json()
            unanonymized_return_model_as_string = self._anonymizer.deanonymize(
                return_model_as_string, anonymization_mapping
            )
            result = SummaryResultModel.model_validate_json(
                unanonymized_return_model_as_string
            )
        else:
            result = llm_completion.structured

        # Guard against LLM returning a different number of summaries than records
        # submitted and replace model-provided IDs with authoritative input IDs.
        aligned_summaries = self._align_record_items(
            request_records=request.feedback_records,
            llm_items=result.feedback_record_summaries,
            align_item=lambda record_id, summary, _index: summary.model_copy(
                update={"id": record_id}
            ),
        )
        result = result.model_copy(
            update={"feedback_record_summaries": aligned_summaries}
        )
        return result

    def _align_record_items(
        self,
        *,
        request_records: tuple[FeedbackRecordModel, ...],
        llm_items: tuple[_AlignedItemT, ...],
        align_item: Callable[[str, _AlignedItemT, int], _AlignedItemT],
    ) -> tuple[_AlignedItemT, ...]:
        """Align LLM result items to input record IDs by request order.

        The LLM is not authoritative for per-record IDs; request IDs are. This
        helper applies a caller-provided item mapper against request IDs.

        Assumptions:
        * request order is the canonical order for outputs
        """
        if len(llm_items) != len(request_records):
            raise AnalysisError(
                f"LLM returned {len(llm_items)} result items"
                f" for {len(request_records)} requested feedback records."
            )

        return tuple(
            align_item(
                record.id,
                llm_items[idx],
                idx,
            )
            for idx, record in enumerate(request_records)
        )

    async def summarize_aggregate(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> AggregateSummaryResultModel:
        """Summarize multiple feedback records as a single aggregate summary.

        Parameters
        ----------
        request : SummaryRequest
            The summarization request containing feedback records and options.
        deadline : datetime
            Absolute UTC deadline by which summarization must complete.

        Returns
        -------
        AggregateSummaryResult
            A single aggregate summary with themes ordered by frequency.
        """
        system_message = _DEFAULT_AGGREGATE_SUMMARIZATION_PROMPT
        if request.output_language:
            system_message += (
                f"\nWrite the title and summary in {request.output_language}."
            )
        if request.prompt:
            system_message += f"\nAdditional instructions: {request.prompt}"

        user_message = "\n\n".join(
            f"{idx}. {record.text}"
            for idx, record in enumerate(request.feedback_records, start=1)
        )

        anonymized_user_message = user_message
        if anonymize:
            anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
                user_message
            )

        timeout = self._check_deadline_and_get_timeout(deadline)
        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=AggregateSummaryResultModel,
            timeout=timeout,
        )
        total_cost = response.cost

        judge_user_message = anonymized_user_message if anonymize else user_message
        judge_system = _build_judge_system_message(
            judge_user_message, response.structured.summary
        )

        judge_timeout = self._check_deadline_and_get_timeout(deadline)
        judge_response = await self._llm.complete(
            system_message=judge_system,
            user_message=_JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        total_cost += judge_response.cost
        quality_score = _parse_judge_quality_score(judge_response.structured)

        response.structured.quality_score = quality_score
        response.structured.ids = tuple(
            record.id for record in request.feedback_records
        )

        if anonymize:
            return_model_as_string = response.structured.model_dump_json()
            unanonymized_return_model_as_string = self._anonymizer.deanonymize(
                return_model_as_string, anonymization_mapping
            )
            return AggregateSummaryResultModel.model_validate_json(
                unanonymized_return_model_as_string
            )

        return response.structured

    async def assign_codes(
        self,
        request: CodingAssignmentRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> CodingAssignmentResultModel:
        """Assign hierarchical codes to each feedback record.

        Parameters
        ----------
        request : CodingAssignmentRequest
            Feedback records, coding framework, ``max_codes``, and tenant id.
        deadline : datetime
            Absolute UTC deadline by which all records must be coded.

        Returns
        -------
        CodingAssignmentResult
            Per-record leaf codes from ``classify_feedback``.

        Raises
        ------
        AnalysisTimeoutError
            When ``deadline`` is reached before every record is processed.
        LLMTimeoutError
            When a single LLM completion exceeds the configured timeout.
        LLMRateLimitError
            When the LLM provider returns rate limiting.
        LLMError
            For other LLM provider failures.
        """
        coded: list[CodedFeedbackRecordModel] = []
        types = request.coding_framework.get("types") or []
        threshold = request.confidence_threshold

        for feedback_record in request.feedback_records:
            self._check_coding_deadline(deadline)

            candidates: list[_ScoredCode] = []

            type_indices = await self._pick_code_indices(
                feedback_text=feedback_record.text,
                current_level="Types",
                entries=types,
                hierarchy_path=None,
                tenant_id=request.tenant_id,
                deadline=deadline,
                anonymize=anonymize,
            )

            for type_index in type_indices:
                type_entry = types[type_index]
                type_name = str(type_entry.get("name", ""))

                judge_type = await self._judge_code_level(
                    feedback_text=feedback_record.text,
                    level="Type",
                    path=[("Type", type_name)],
                    tenant_id=request.tenant_id,
                    deadline=deadline,
                    anonymize=anonymize,
                )
                if threshold is not None and judge_type.score < threshold:
                    continue

                categories = type_entry.get("categories") or []
                category_indices = await self._pick_code_indices(
                    feedback_text=feedback_record.text,
                    current_level="Categories",
                    entries=categories,
                    hierarchy_path=[("Type", type_name)],
                    tenant_id=request.tenant_id,
                    deadline=deadline,
                    anonymize=anonymize,
                )

                for category_index in category_indices:
                    category = categories[category_index]
                    category_name = str(category.get("name", ""))

                    judge_category = await self._judge_code_level(
                        feedback_text=feedback_record.text,
                        level="Category",
                        path=[("Type", type_name), ("Category", category_name)],
                        tenant_id=request.tenant_id,
                        deadline=deadline,
                        anonymize=anonymize,
                    )
                    if threshold is not None and judge_category.score < threshold:
                        continue

                    codes = category.get("codes") or []
                    code_indices = await self._pick_code_indices(
                        feedback_text=feedback_record.text,
                        current_level="Codes",
                        entries=codes,
                        hierarchy_path=[
                            ("Type", type_name),
                            ("Category", category_name),
                        ],
                        tenant_id=request.tenant_id,
                        deadline=deadline,
                        anonymize=anonymize,
                    )

                    for code_index in code_indices:
                        code = codes[code_index]
                        code_name = str(code.get("name", ""))

                        judge_code = await self._judge_code_level(
                            feedback_text=feedback_record.text,
                            level="Code",
                            path=[
                                ("Type", type_name),
                                ("Category", category_name),
                                ("Code", code_name),
                            ],
                            tenant_id=request.tenant_id,
                            deadline=deadline,
                            anonymize=anonymize,
                        )
                        if threshold is not None and judge_code.score < threshold:
                            continue

                        candidates.append(
                            _ScoredCode(
                                code_id=str(code.get("code_id", "")),
                                code_label=code_name,
                                confidence_type=judge_type.score,
                                confidence_category=judge_category.score,
                                confidence_code=judge_code.score,
                                explanation_type=judge_type.explanation,
                                explanation_category=judge_category.explanation,
                                explanation_code=judge_code.explanation,
                            )
                        )

            candidates.sort(key=lambda c: c.confidence_aggregate, reverse=True)
            top = candidates[: request.max_codes]

            coded.append(
                CodedFeedbackRecordModel(
                    feedback_record_id=feedback_record.id,
                    assigned_codes=tuple(
                        AssignedCodeModel(
                            code_id=c.code_id,
                            code_label=c.code_label,
                            confidence_type=c.confidence_type,
                            confidence_category=c.confidence_category,
                            confidence_code=c.confidence_code,
                            confidence_aggregate=c.confidence_aggregate,
                            explanation=c.explanation,
                        )
                        for c in top
                    ),
                )
            )

        return CodingAssignmentResultModel(coded_feedback_records=tuple(coded))

    async def detect_sensitive_content(
        self,
        request: SensitivityAnalysisRequestModel,
        deadline: datetime,
        anonymize: bool = True,
    ) -> SensitivityAnalysisResultModelList:
        """Detect sensitive content in feedback records.

        Parameters
        ----------
        request : SensitivityAnalysisRequestModel
            The sensitivity analysis request containing feedback records and tenant id.

        Returns
        -------
        SensitivityAnalysisResultModelList
            The sensitivity analysis results for each feedback record.
        """
        timeout = self._check_deadline_and_get_timeout(deadline)
        system_message = _DEFAULT_SENSITIVITY_DETECTION_PROMPT
        user_message = str(request.feedback_records)

        anonymized_user_message = user_message
        if anonymize:
            anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
                user_message
            )

        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SensitivityAnalysisResultModelList,
            timeout=timeout,
        )

        structured = response.structured
        if anonymize:
            return_model_as_string = structured.model_dump_json()
            unanonymized_return_model_as_string = self._anonymizer.deanonymize(
                return_model_as_string, anonymization_mapping
            )
            structured = SensitivityAnalysisResultModelList.model_validate_json(
                unanonymized_return_model_as_string
            )

        aligned_results = tuple(
            SensitivityAnalysisResultModel(
                feedback_record_id=record.id,
                sensitivity_types=(
                    structured.results[idx].sensitivity_types
                    if idx < len(structured.results)
                    else ()
                ),
                explanation=(
                    structured.results[idx].explanation
                    if idx < len(structured.results)
                    else "No sensitive content detected."
                ),
            )
            for idx, record in enumerate(request.feedback_records)
        )
        return SensitivityAnalysisResultModelList(results=aligned_results)

    def _check_deadline_and_get_timeout(self, deadline: datetime) -> float:
        """Raise if the deadline has passed or too little time remains.

        Return a timeout (seconds) bounded by the deadline and the
        configured per-call limit.
        """
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise AnalysisTimeoutError("Deadline exceeded")
        if remaining < _MINIMUM_ATTEMPT_WINDOW:
            raise AnalysisTimeoutError(
                f"Insufficient time remaining ({remaining:.1f}s) for an LLM attempt"
            )
        return min(self._llm_timeout_seconds, remaining)

    def _check_coding_deadline(self, deadline: datetime) -> None:
        """Raise when the coding deadline is exceeded."""
        if datetime.now(UTC) >= deadline:
            raise AnalysisTimeoutError(
                "Coding deadline exceeded before all feedback records were processed"
            )

    async def _pick_code_indices(
        self,
        *,
        feedback_text: str,
        current_level: str,
        entries: list[dict],
        hierarchy_path: list[tuple[str, str]] | None,
        tenant_id: str,
        deadline: datetime,
        anonymize: bool = True,
    ) -> list[int]:
        """Build one coding prompt, call the LLM, and parse selected indices."""
        labels = [str(entry.get("name", "")) for entry in entries]
        system_message, user_message = build_pick_messages(
            feedback_text=feedback_text,
            current_level=current_level,
            labels=labels,
            hierarchy_path=hierarchy_path,
        )
        if not user_message:
            return []

        self._check_coding_deadline(deadline)
        self._check_token_limit(system_message, user_message)

        anonymized_user_message = user_message
        if anonymize:
            anonymized_user_message, _ = self._anonymizer.anonymize(user_message)

        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=tenant_id,
            response_model=str,
        )
        return parse_selected_indices(response.structured, len(labels))

    async def _judge_code_level(
        self,
        *,
        feedback_text: str,
        level: str,
        path: list[tuple[str, str]],
        tenant_id: str,
        deadline: datetime,
        anonymize: bool,
    ) -> JudgeResponse:
        """Call the judge LLM for one hierarchy level; return structured score and explanation."""
        system_message, user_message = build_judge_messages(
            feedback_text=feedback_text,
            level=level,
            path=path,
        )
        self._check_coding_deadline(deadline)
        self._check_token_limit(system_message, user_message)
        if anonymize:
            user_message, _ = self._anonymizer.anonymize(user_message)
        response = await self._llm.complete(
            system_message=system_message,
            user_message=user_message,
            tenant_id=tenant_id,
            response_model=JudgeResponse,
        )
        if not 0.0 <= response.structured.score <= 1.0:
            raise AnalysisError("LLM judge returned score outside 0.0-1.0")
        return response.structured

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def _check_token_limit(self, system_message: str, user_message: str) -> None:
        """Estimate total tokens and raise if over the limit.

        Parameters
        ----------
        system_message : str
            The assembled system message.
        user_message : str
            The assembled user message containing the feedback records.

        Raises
        ------
        FeedbackTooLargeError
            When estimated tokens exceed the configured limit.
        """
        assembled_text = system_message + user_message
        estimated_tokens = len(assembled_text) // self._settings.chars_per_token
        if estimated_tokens > self._max_total_tokens:
            msg = (
                f"Estimated tokens ({estimated_tokens}) exceed limit "
                f"({self._max_total_tokens})"
            )
            raise FeedbackTooLargeError(
                msg,
                estimated_tokens=estimated_tokens,
                limit=self._max_total_tokens,
            )
