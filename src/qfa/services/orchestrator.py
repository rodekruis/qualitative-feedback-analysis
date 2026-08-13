"""Orchestrator service — core business logic for feedback analysis.

Assembles prompts, enforces token limits, filters prompt injection,
manages retries with exponential backoff, and enforces deadlines.

The scaffolding those last three share across use cases — anonymise a batch
of records, derive a per-call timeout from the deadline, guard the token
budget, run a semaphore-bounded completion — lives on the injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` rather than on this
class (ADR-017).

The assign-codes and analyse use cases have already been extracted out of
this class into :class:`~qfa.services.coding.CodingService` and
:class:`~qfa.services.analyze.AnalyzeService` respectively; only
``summarize`` and ``summarize_aggregate`` remain, and the class itself is
deleted in #267.
"""

import logging
from datetime import datetime

from qfa.domain.errors import AnalysisError
from qfa.domain.models import (
    AggregateSummaryResultModel,
    FeedbackRecordSummaryModel,
    SingleSummaryRequestModel,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.ports import AnonymizationPort, LLMPort
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompts import (
    JUDGE_USER_MESSAGE,
    build_feedback_record_envelope,
    build_feedback_records_envelope,
    build_output_language_instruction,
)
from qfa.services.record_links import hyperlink_form_references
from qfa.settings import OrchestratorSettings

logger = logging.getLogger(__name__)

_DEFAULT_SUMMARIZATION_PROMPT = (
    "Summarize the feedback item as concise bullet points.\n"
    "Strict Constraint: The summary must be extremely concise, using no more than 3-5 brief bullet points.\n"
    "Constraint: Each bullet point should be a single sentence fragment focusing only on the core sentiment or issue.\n"
    "Also create a short, 3-5 word descriptive title.\n"
    "Do not include markdown code fences.\n"
    "Do not end with a question, an offer of further help, or an invitation for follow-up input.\n"
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
    "Do not end with a question, an offer of further help, or an invitation for follow-up input.\n"
    "Use the same language as the input feedback records unless a target language is specified."
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


class Orchestrator:
    """Core orchestration service for feedback analysis.

    Assembles prompts from feedback records, validates input,
    calls the LLM through the ``LLMPort``, and manages retries
    with exponential backoff and deadline enforcement.

    Deadline arithmetic, the token-budget guard, batch anonymisation and
    semaphore-bounded completions are delegated to the injected
    :class:`~qfa.services.llm_call_executor.LLMCallExecutor` (``executor``
    below), so this class holds use-case logic rather than call scaffolding.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter used for every generation call
        (summarisation, sensitivity detection, code assignment).
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before LLM calls.
    settings : OrchestratorSettings
        Cross-cutting orchestrator configuration (retry policy, token
        budget estimation, metadata allow-list).
    llm_timeout_seconds : float
        Maximum time in seconds for a single LLM call.
    max_total_tokens : int
        Maximum estimated total tokens for a single request.
    judge_llm : LLMPort | None
        Optional separate adapter for the LLM-as-judge quality-score calls,
        so judging can run on a different model than generation. ``None``
        (the default) routes judge calls to ``llm``, which is the behaviour
        when no ``JUDGE_LLM_MODEL`` is configured. Configured via
        ``JUDGE_LLM_*`` and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.

        Two call sites on this class use it: the judges in
        ``summarize_aggregate`` and ``summarize``. The analyse judge and the
        hierarchical leaf judges are two of the others, and live on
        :class:`~qfa.services.analyze.AnalyzeService`. The per-level judge in
        :class:`~qfa.services.coding.CodingService` deliberately stays on the
        primary connection, which is why that service takes no judge client
        at all.
    executor : LLMCallExecutor | None
        The shared LLM-call scaffolding (anonymise-records, deadline→timeout
        derivation, token-budget guard, semaphore-bounded completion) this
        orchestrator delegates to, per ADR-017. The composition root
        (:func:`qfa.api.composition.build_services`) constructs it
        explicitly and shares the one instance with every use-case service.
        ``None`` (the default) builds one over the same ``llm``,
        ``anonymizer``, ``settings``, ``llm_timeout_seconds`` and
        ``max_total_tokens`` this constructor already received, so callers
        that don't care about the collaborator — scripts, notebooks, and the
        bulk of the test suite — need not thread it through.
    """

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        settings: OrchestratorSettings,
        llm_timeout_seconds: float,
        max_total_tokens: int,
        judge_llm: LLMPort | None = None,
        executor: LLMCallExecutor | None = None,
    ) -> None:
        self._llm = llm
        # Judge calls run on their own connection when one is configured, so
        # the generator does not grade its own output. Falling back to the
        # primary client keeps the default (no JUDGE_LLM_MODEL) behaviour
        # identical to before the judge connection existed, and means call
        # sites never branch — they just use _judge_llm.
        self._judge_llm = judge_llm if judge_llm is not None else llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._settings = settings
        self._llm_timeout_seconds = llm_timeout_seconds
        self._max_total_tokens = max_total_tokens
        # Shared LLM-call scaffolding (ADR-017): an injected collaborator, not a
        # base class. Default-constructed from the arguments above so the
        # composition root can inject one without every other construction site
        # having to. Either way it is built over the *primary* llm; judge calls
        # pass ``llm=self._judge_llm`` per call.
        self._executor = executor or LLMCallExecutor(
            llm=llm,
            anonymizer=anonymizer,
            settings=settings,
            llm_timeout_seconds=llm_timeout_seconds,
            max_total_tokens=max_total_tokens,
        )

    async def summarize_bulk(
        self,
        request: SummaryRequestModel,
        deadline: datetime,
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
        system_message += build_output_language_instruction(
            request.output_language, subject="title and summary"
        )
        if request.prompt:
            system_message += f"\nAdditional instructions: {request.prompt}"

        user_message = build_feedback_records_envelope(
            request.feedback_records, include_metadata=False
        )

        anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
            user_message
        )

        timeout = self._executor.check_deadline_and_get_timeout(deadline)
        response = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=AggregateSummaryResultModel,
            timeout=timeout,
        )
        total_cost = response.cost

        judge_system = _build_judge_system_message(
            anonymized_user_message, response.structured.summary
        )

        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        judge_response = await self._judge_llm.complete(
            system_message=judge_system,
            user_message=JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        total_cost += judge_response.cost
        quality_score = _parse_judge_quality_score(judge_response.structured)

        response.structured.quality_score = quality_score

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        result = AggregateSummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )
        return result.model_copy(
            update={
                "summary": hyperlink_form_references(
                    result.summary,
                    request.feedback_records,
                    request.espo_feedback_base_url,
                )
            }
        )

    async def summarize(
        self,
        request: SingleSummaryRequestModel,
        deadline: datetime,
    ) -> FeedbackRecordSummaryModel:
        """Summarize a single feedback record.

        Parameters
        ----------
        request : SingleSummaryRequestModel
            The summarization request containing a single feedback record.
        deadline : datetime
            Absolute UTC deadline by which summarization must complete.

        Returns
        -------
        FeedbackRecordSummaryModel
            The summary title and content for the feedback record.

        Raises
        ------
        AnalysisError
            When the LLM returns invalid output or another non-recoverable
            error occurs.
        """
        timeout = self._executor.check_deadline_and_get_timeout(deadline)
        system_message = _DEFAULT_SUMMARIZATION_PROMPT

        user_message = build_feedback_record_envelope(
            request.feedback_record, include_metadata=False
        )
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

        if not llm_completion.structured.feedback_record_summaries:
            raise AnalysisError("LLM returned no summaries for the feedback record.")

        judge_system = _build_judge_system_message(
            anonymized_user_message,
            llm_completion.structured.feedback_record_summaries[0].summary,
        )
        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        judge_response = await self._judge_llm.complete(
            system_message=judge_system,
            user_message=JUDGE_USER_MESSAGE,
            tenant_id=request.tenant_id,
            response_model=str,
            timeout=judge_timeout,
        )
        quality_score = _parse_judge_quality_score(judge_response.structured)

        return_model_as_string = llm_completion.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        result = SummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )

        return result.feedback_record_summaries[0].model_copy(
            update={"id": request.feedback_record.id, "quality_score": quality_score}
        )
