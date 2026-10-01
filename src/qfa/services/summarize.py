"""Summarisation use cases — one aggregate summary, or one per record.

:class:`SummarizeService` owns the two summarisation use cases extracted
from ``Orchestrator`` (issue #264, epic #112): ``summarize_bulk`` behind
``POST /v1/summarize-bulk`` and ``summarize`` behind ``POST /v1/summarize``.
They are the clearest natural pair in the old god-class — both are a single
generation call followed by a free-text judge call, differing only in bulk
vs. single-record shape — so their prompts and hyperlinking conventions now
sit together in one file a reader can hold in their head.

Per ADR-017 this is a plain class with **no base class**: the shared
LLM-call scaffolding arrives as an injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` collaborator, and
inheritance in this codebase means port↔adapter conformance and nothing
else. The constructor names exactly the dependencies these two use cases
need — no embedder, no analyze settings, no token ceiling (neither path
runs the pre-flight budget guard).

Two module-level helpers these methods use are deliberately **not** defined
here: ``hyperlink_form_references`` (also used by ``analyze_bulk`` and the
hierarchical reduce) and ``JUDGE_USER_MESSAGE`` (also used by the analyse
and hierarchical leaf judges). They live in :mod:`qfa.services.record_links`
and :mod:`qfa.services.prompts` respectively, shared with their other users.
"""

import logging
from datetime import datetime

from qfa.domain.errors import AnalysisError
from qfa.domain.models import (
    AggregateSummaryResultModel,
    CommunityMeetingRecordSummaryModel,
    FeedbackRecordSummaryModel,
    JudgeComponents,
    SingleSummaryCommunityMeetingRequestModel,
    SingleSummaryRequestModel,
    SummaryCommunityMeetingResultModel,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.ports import AnonymizationPort, EvaluationPort, LLMPort
from qfa.services.call_context import judge_call
from qfa.services.judge_scoring import (
    log_judge_components,
    parse_judge_components,
    record_judge_scores,
)
from qfa.services.language import detect_source_language
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompts import (
    JUDGE_USER_MESSAGE,
    build_community_meeting_record_envelope,
    build_feedback_record_envelope,
    build_feedback_records_envelope,
    build_output_language_instruction,
)
from qfa.services.record_links import hyperlink_form_references

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
_DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT = (
    "Summarize the Community Meeting notes as concise bullet points.\n"
    "Strict Constraint: The summary must be  concise, using no more than 5-10 bullet points.\n"
    "Constraint: Each bullet point should be a single sentence fragment focusing only on the core sentiment or issue.\n"
    "Also create a short, 3-5 word descriptive title.\n"
    "Definition of common abbreviations: fgd means focus group discussion and kii means key informant interview.\n"
    "Do not include markdown code fences.\n"
    "Do not end with a question, an offer of further help, or an invitation for follow-up input.\n"
    "Use the same language as the input Community Meeting item unless a target language is specified."
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
1.0 = fully supported by the source text above, no hallucinations
0.5 = mostly correct, minor issues
0.0 = major inaccuracies

Coverage:
1.0 = captures every key point in the source text above — whether that is
one feedback record's own points, or the recurring themes across a batch
0.5 = partially covers the key points in the source text above
0.0 = misses most of the key points in the source text above

Clarity:
1.0 = very clear and concise
0.5 = somewhat clear
0.0 = confusing or poorly written

Also produce an uncertainty explanation — one short paragraph explaining
which criterion drove the score, calling out unsupported claims if any.

Output format:
Respond with exactly four lines and nothing else, in this exact format:
FAITHFULNESS: <0.0-1.0>
COVERAGE: <0.0-1.0>
CLARITY: <0.0-1.0>
UNCERTAINTY_EXPLANATION: <one short paragraph>

Example:
FAITHFULNESS: 0.9
COVERAGE: 0.8
CLARITY: 0.9
UNCERTAINTY_EXPLANATION: The summary is well-supported by the source text and captures its key points, though it slightly overstates how significant one minor detail is.
"""


def _parse_judge_quality_score(raw: str) -> JudgeComponents:
    """Parse the judge's FAITHFULNESS/COVERAGE/CLARITY reply into components and total.

    Delegates to the shared :func:`~qfa.services.judge_scoring.parse_judge_components`
    (also used by the analyse judge, #351), so an out-of-range component raises
    ``AnalysisError`` here too, never a ``ValidationError`` that would escape as
    a 500. The returned :class:`JudgeComponents` carries the derived total via
    its ``quality_score`` property.
    """
    return parse_judge_components(raw)


def _build_judge_system_message(source_text: str, summary: str) -> str:
    """Fill the judge prompt with the provided source text and summary."""
    return _JUDGE_PROMPT.format(source_text=source_text, summary=summary)


class SummarizeService:
    """Summarisation use cases: one aggregate summary, or one per record.

    Both methods issue two LLM calls — the summary itself on ``llm``, then a
    free-text judge call on ``judge_llm`` that reports faithfulness/coverage/
    clarity, from which ``quality_score`` is derived.

    Parameters
    ----------
    llm : LLMPort
        The LLM provider adapter used for the summary generation calls.
    anonymizer : AnonymizationPort
        The anonymisation adapter used to redact PII before the LLM calls
        and to restore it in the result.
    executor : LLMCallExecutor
        The shared LLM-call scaffolding (ADR-017): an injected collaborator,
        never a base class. These two use cases consult it for the
        deadline→timeout derivation; the composition root
        (:func:`qfa.api.composition.build_services`) hands over the
        *same* instance every other service holds.
    judge_llm : LLMPort | None
        Optional separate adapter for the LLM-as-judge quality-score calls,
        so the model that writes a summary does not grade it. ``None`` (the
        default) routes judge calls to ``llm``, which is the behaviour when
        no ``JUDGE_LLM_MODEL`` is configured. Configured via ``JUDGE_LLM_*``
        and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.
    evaluator : EvaluationPort | None
        Where judge components are sent as live Langfuse scores (#354).
        ``None`` (the default) means every judge call's scores are simply
        not sent — the composition root
        (:func:`qfa.api.composition.build_services`) always injects a
        real port, a no-op one when Langfuse is unconfigured, so ``None``
        here is only ever a test/script default, never production
        behaviour.
    """

    def __init__(
        self,
        llm: LLMPort,
        anonymizer: AnonymizationPort,
        executor: LLMCallExecutor,
        judge_llm: LLMPort | None = None,
        evaluator: EvaluationPort | None = None,
    ) -> None:
        self._llm = llm
        # Falling back to the primary client keeps the default (no
        # JUDGE_LLM_MODEL) behaviour identical to before the judge connection
        # existed, and means call sites never branch — they just use
        # _judge_llm.
        self._judge_llm = judge_llm if judge_llm is not None else llm
        self._anonymizer: AnonymizationPort = anonymizer
        self._executor = executor
        self._evaluator = evaluator

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

        judge_system = _build_judge_system_message(
            anonymized_user_message, response.structured.summary
        )

        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        with judge_call():
            judge_response = await self._judge_llm.complete(
                system_message=judge_system,
                user_message=JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=str,
                timeout=judge_timeout,
            )
        components = _parse_judge_quality_score(judge_response.structured)
        log_judge_components(logger, components)
        record_judge_scores(self._evaluator, components)

        response.structured.quality_score = components.quality_score
        response.structured.components = components

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

        Notes
        -----
        The output language is auto-detected from the record's own content
        and pinned in the system message — there is no request field for it
        (#294).
        """
        timeout = self._executor.check_deadline_and_get_timeout(deadline)
        # Detected on the raw content, before the envelope tags and the
        # anonymiser's ``<PERSON_0>`` placeholders dilute the sample. Returns
        # an empty suffix when detection declines, leaving the prompt's own
        # soft "same language as the input" line to stand.
        detected_language = detect_source_language(request.feedback_record.content)
        system_message = (
            _DEFAULT_SUMMARIZATION_PROMPT
            + build_output_language_instruction(
                detected_language, subject="title and summary"
            )
        )

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
        with judge_call():
            judge_response = await self._judge_llm.complete(
                system_message=judge_system,
                user_message=JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=str,
                timeout=judge_timeout,
            )
        components = _parse_judge_quality_score(judge_response.structured)
        log_judge_components(logger, components)
        record_judge_scores(self._evaluator, components)

        return_model_as_string = llm_completion.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        result = SummaryResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )

        return result.feedback_record_summaries[0].model_copy(
            update={
                "id": request.feedback_record.id,
                "quality_score": components.quality_score,
                "components": components,
            }
        )

    async def summarize_community_meeting(
        self,
        request: SingleSummaryCommunityMeetingRequestModel,
        deadline: datetime,
    ) -> CommunityMeetingRecordSummaryModel:
        """Summarize a single community meeting record."""
        timeout = self._executor.check_deadline_and_get_timeout(deadline)
        detected_language = detect_source_language(
            request.community_meeting_record.meetingNotes
        )
        system_message = (
            _DEFAULT_SUMMARIZATION_COMMUNITY_MEETING_PROMPT
            + build_output_language_instruction(
                detected_language, subject="title and summary"
            )
        )

        user_message = build_community_meeting_record_envelope(
            request.community_meeting_record, include_metadata=True
        )
        anonymized_user_message, anonymization_mapping = self._anonymizer.anonymize(
            user_message
        )

        llm_completion = await self._llm.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SummaryCommunityMeetingResultModel,
            timeout=timeout,
        )

        if not llm_completion.structured.community_meeting_record_summaries:
            raise AnalysisError(
                "LLM returned no summaries for the community meeting record."
            )

        judge_system = _build_judge_system_message(
            anonymized_user_message,
            llm_completion.structured.community_meeting_record_summaries[0].summary,
        )
        judge_timeout = self._executor.check_deadline_and_get_timeout(deadline)
        with judge_call():
            judge_response = await self._judge_llm.complete(
                system_message=judge_system,
                user_message=JUDGE_USER_MESSAGE,
                tenant_id=request.tenant_id,
                response_model=str,
                timeout=judge_timeout,
            )
        components = _parse_judge_quality_score(judge_response.structured)
        log_judge_components(logger, components)

        return_model_as_string = llm_completion.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        result = SummaryCommunityMeetingResultModel.model_validate_json(
            unanonymized_return_model_as_string
        )

        return result.community_meeting_record_summaries[0].model_copy(
            update={
                "id": request.community_meeting_record.id,
                "summary": hyperlink_form_references(
                    result.community_meeting_record_summaries[0].summary,
                    (request.community_meeting_record,),
                    request.espo_feedback_base_url,
                ),
                "quality_score": components.quality_score,
                "components": components,
            }
        )
