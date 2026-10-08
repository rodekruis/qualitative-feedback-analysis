"""Sensitivity-detection use case.

Two LLM calls per feedback record: a classifier call against the
:class:`~qfa.domain.sensitivity_types.SensitivityType` vocabulary, then an
LLM-as-judge call scoring how confident it is that the classification is
right. The judge runs on *every* record, not only the flagged ones — a
sensitive record wrongly let through is the expensive error here, and a
judge that only sees flags can never catch it.

Per ADR-017 this class has **no base class**: the LLM-call scaffolding it
shares with the other use cases (anonymise, deadline→timeout, the call
itself, deanonymise) is the injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` it delegates to,
not a superclass it inherits from.
"""

import logging
from datetime import datetime

from pydantic import ValidationError

from qfa.domain.errors import (
    AnalysisError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from qfa.domain.models import (
    FeedbackRecordModel,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityClassificationModel,
    SensitivityClassificationModelList,
)
from qfa.domain.ports import EvaluationPort, LLMPort
from qfa.domain.sensitivity_types import SENSITIVITY_TYPE_DESCRIPTIONS
from qfa.services.call_context import judge_call
from qfa.services.judge_scoring import (
    parse_judge_response,
    record_sensitivity_judge_score,
)
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompt_names import (
    SENSITIVITY_DETECTION_JUDGE,
    SENSITIVITY_DETECTION_SYSTEM,
    prompt_ref,
)
from qfa.services.prompts import build_feedback_record_envelope

logger = logging.getLogger(__name__)

_SENSITIVITY_TYPE_GUIDANCE = "\n".join(
    f"- {sensitivity_type.value}: {description}"
    for sensitivity_type, description in SENSITIVITY_TYPE_DESCRIPTIONS.items()
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


_JUDGE_PROMPT = (
    "You are evaluating whether a sensitivity classification of a community "
    "feedback record is correct.\n\n"
    "Context:\n"
    "These feedback records are collected from community members by Red Cross / "
    "Red Crescent National Societies as part of humanitarian programs. Feedback "
    "is qualitative and unstructured: often short, indirect, emotionally "
    "expressed, or translated from a local language.\n\n"
    "Your task:\n"
    "A classifier has labelled the record with zero or more sensitivity types "
    "and given a reason. Score how confident you are that the classification is "
    "correct. A label of no sensitive content is a classification like any "
    "other: score it the same way, where a high score means you are confident "
    "nothing sensitive was missed.\n\n"
    f"SensitivityType guidance:\n{_SENSITIVITY_TYPE_GUIDANCE}\n\n"
    "Important:\n"
    "- Judge the classification, not how well the explanation is written.\n"
    "- Do not penalise the record for being brief or colloquial.\n"
    "- This service deliberately prefers false positives over false negatives, "
    "so a defensible flag on ambiguous feedback is not wrong; a missed "
    "sensitive record is.\n"
    "- Names and places may appear as placeholders such as ``<PERSON_0>``. "
    "Treat them as if the real value were present.\n\n"
    "Scoring:\n"
    "Assign a score from 0.0 to 1.0. Use the full continuous range — do not "
    "round to fixed values.\n\n"
    "Reference anchors:\n"
    "- 1.0: the classification is clearly correct\n"
    "- 0.75: the classification is reasonable\n"
    "- 0.5: the classification is plausible but uncertain\n"
    "- 0.25: the classification is probably wrong\n"
    "- 0.0: the classification is clearly wrong\n\n"
    "Output format:\n"
    "Respond with exactly two lines and nothing else, in this exact format:\n"
    "SCORE: <a number between 0.0 and 1.0>\n"
    "EXPLANATION: <at most two sentences>\n\n"
    "Example:\n"
    "SCORE: 0.9\n"
    "EXPLANATION: The record describes a staff member demanding payment in "
    "exchange for aid, which the corruption label captures directly."
)

_NO_TYPES_LABEL = "none (not sensitive)"


def build_sensitivity_judge_user_message(
    feedback_record: FeedbackRecordModel,
    classification: SensitivityClassificationModel,
) -> str:
    """Assemble the judge's user message: the record plus the classification.

    Metadata and the record id are left out — the judge scores the fit
    between text and labels, and neither helps with that.
    """
    types = (
        ", ".join(t.value for t in classification.sensitivity_types) or _NO_TYPES_LABEL
    )
    envelope = build_feedback_record_envelope(
        feedback_record, include_metadata=False, include_id=False
    )
    return (
        f"{envelope}\n"
        f"<classification>\n"
        f"sensitivity_types: {types}\n"
        f"explanation: {classification.explanation}\n"
        f"</classification>\n\n"
        f"<instruction>\nEvaluate this classification.\n</instruction>"
    )


class SensitivityService:
    """Detect sensitive content in a single feedback record.

    Parameters
    ----------
    executor : LLMCallExecutor
        The shared LLM-call scaffolding this use case delegates to:
        anonymisation of the outgoing message, the deadline-bounded call
        itself, and restoration of the redacted values in the response. The
        composition root (:func:`qfa.api.composition.build_services`) hands
        over the same instance the other services use.
    judge_llm : LLMPort | None
        Optional separate adapter for the judge call, so judging can run on
        a different model than the classification. ``None`` (the default)
        routes the judge call to the executor's own client, which is the
        behaviour when no ``JUDGE_LLM_MODEL`` is configured. Configured via
        ``JUDGE_LLM_*`` and resolved in
        :func:`qfa.api.composition.resolve_judge_llm_settings`.
    evaluator : EvaluationPort | None
        Where the judge's confidence is sent as a live Langfuse score, via
        :func:`~qfa.services.judge_scoring.record_sensitivity_judge_score`.
        ``None`` (the default) means the score is simply not recorded.
    prompt_versions : dict[str, int] | None
        Current Langfuse version per name in
        :data:`~qfa.services.prompt_registry.SYSTEM_PROMPTS` (#398). ``None``
        (the default) is treated as ``{}``, so the LLM calls this service
        makes simply carry no prompt-version span attribute, the same as a
        name absent because Langfuse is unconfigured or its push failed.
    """

    def __init__(
        self,
        executor: LLMCallExecutor,
        judge_llm: LLMPort | None = None,
        evaluator: EvaluationPort | None = None,
        prompt_versions: dict[str, int] | None = None,
    ) -> None:
        self._executor = executor
        self._judge_llm = judge_llm
        self._evaluator = evaluator
        self._prompt_versions: dict[str, int] = prompt_versions or {}

    async def detect_sensitive_content(
        self,
        request: SensitivityAnalysisRequestModel,
        deadline: datetime,
    ) -> SensitivityAnalysisResultModel:
        """Detect sensitive content in a single feedback record.

        Two LLM calls are issued: the classification itself, then a judge
        call scoring it. The judge runs whether or not the record was
        flagged. If the judge call fails the result is still returned, with
        ``confidence=None``.

        Parameters
        ----------
        request : SensitivityAnalysisRequestModel
            The sensitivity analysis request containing a single feedback record.
        deadline : datetime
            The wall-clock deadline for the whole request; both LLM calls are
            timed out against whatever remains of it.

        Returns
        -------
        SensitivityAnalysisResultModel
            The sensitivity analysis result for the feedback record.
        """
        system_message = _DEFAULT_SENSITIVITY_DETECTION_PROMPT
        user_message = build_feedback_record_envelope(
            request.feedback_record, include_metadata=True, include_id=True
        )

        anonymized_user_message, anonymization_mapping = self._executor.anonymize_text(
            user_message
        )

        response = await self._executor.complete(
            system_message=system_message,
            user_message=anonymized_user_message,
            tenant_id=request.tenant_id,
            response_model=SensitivityClassificationModelList,
            deadline=deadline,
            prompt=prompt_ref(self._prompt_versions, SENSITIVITY_DETECTION_SYSTEM),
        )

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        structured = SensitivityClassificationModelList.model_validate_json(
            unanonymized_return_model_as_string
        )

        raw = structured.results[0] if structured.results else None
        classification = SensitivityClassificationModel(
            feedback_record_id=request.feedback_record.id,
            sensitivity_types=raw.sensitivity_types if raw else (),
            explanation=raw.explanation if raw else "No sensitive content detected.",
        )

        confidence = await self._judge_classification(
            feedback_record=request.feedback_record,
            classification=classification,
            tenant_id=request.tenant_id,
            deadline=deadline,
        )

        return SensitivityAnalysisResultModel(
            feedback_record_id=classification.feedback_record_id,
            sensitivity_types=classification.sensitivity_types,
            explanation=classification.explanation,
            confidence=confidence,
        )

    async def _judge_classification(
        self,
        *,
        feedback_record: FeedbackRecordModel,
        classification: SensitivityClassificationModel,
        tenant_id: str,
        deadline: datetime,
    ) -> float | None:
        """Score *classification* with the judge LLM; ``None`` when unavailable.

        Free-text, not schema-enforced structured output — see
        :class:`~qfa.services.judge_scoring.JudgeResponse`'s docstring for
        why the judge connection cannot rely on the provider to enforce a
        response schema. Every failure mode the judge can have — a provider
        error, a reply that does not parse, a score outside 0.0-1.0 —
        returns ``None`` rather than raising, so a working classification is
        never thrown away because its grader fell over. A deadline that has
        already passed still raises, because that is the whole request
        running out of time, not the judge failing.
        """
        user_message = build_sensitivity_judge_user_message(
            feedback_record, classification
        )
        anonymized_user_message, _ = self._executor.anonymize_text(user_message)
        try:
            with judge_call():
                response = await self._executor.complete(
                    llm=self._judge_llm,
                    system_message=_JUDGE_PROMPT,
                    user_message=anonymized_user_message,
                    tenant_id=tenant_id,
                    response_model=str,
                    deadline=deadline,
                    prompt=prompt_ref(
                        self._prompt_versions, SENSITIVITY_DETECTION_JUDGE
                    ),
                )
            judged = parse_judge_response(response.structured)
        except (
            LLMError,
            LLMTimeoutError,
            LLMRateLimitError,
            ValidationError,
            AnalysisError,
        ) as exc:
            logger.warning(
                "Sensitivity judge call failed: error_class=%s",
                type(exc).__name__,
            )
            return None

        if not 0.0 <= judged.score <= 1.0:
            logger.warning(
                "Sensitivity judge returned score outside 0.0-1.0: score=%s",
                judged.score,
            )
            return None

        record_sensitivity_judge_score(self._evaluator, score=judged.score)
        return judged.score
