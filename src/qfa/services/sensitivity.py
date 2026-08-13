"""Sensitivity-detection use case.

One LLM call per feedback record, classifying it against the
:class:`~qfa.domain.sensitivity_types.SensitivityType` vocabulary. This is
the smallest of the use cases extracted from ``Orchestrator`` by epic #112:
no embedder, no judge connection, no chunking — which is why its
constructor names exactly one dependency.

Per ADR-017 this class has **no base class**: the LLM-call scaffolding it
shares with the other use cases (anonymise, deadline→timeout, the call
itself, deanonymise) is the injected
:class:`~qfa.services.llm_call_executor.LLMCallExecutor` it delegates to,
not a superclass it inherits from.
"""

from datetime import datetime

from qfa.domain.models import (
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
)
from qfa.domain.sensitivity_types import SENSITIVITY_TYPE_DESCRIPTIONS
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompts import build_feedback_record_envelope

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


class SensitivityService:
    """Detect sensitive content in a single feedback record.

    Parameters
    ----------
    executor : LLMCallExecutor
        The shared LLM-call scaffolding this use case delegates to:
        anonymisation of the outgoing message, the deadline-bounded call
        itself, and restoration of the redacted values in the response. The
        composition root (:func:`qfa.api.composition.build_service_graph`) hands
        over the same instance the other services use.
    """

    def __init__(self, executor: LLMCallExecutor) -> None:
        self._executor = executor

    async def detect_sensitive_content(
        self,
        request: SensitivityAnalysisRequestModel,
        deadline: datetime,
    ) -> SensitivityAnalysisResultModel:
        """Detect sensitive content in a single feedback record.

        Parameters
        ----------
        request : SensitivityAnalysisRequestModel
            The sensitivity analysis request containing a single feedback record.
        deadline : datetime
            The wall-clock deadline for the whole request; the LLM call is
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
            response_model=SensitivityAnalysisResultModelList,
            deadline=deadline,
        )

        return_model_as_string = response.structured.model_dump_json()
        unanonymized_return_model_as_string = self._executor.deanonymize_json(
            return_model_as_string, anonymization_mapping
        )
        structured = SensitivityAnalysisResultModelList.model_validate_json(
            unanonymized_return_model_as_string
        )

        raw = structured.results[0] if structured.results else None
        return SensitivityAnalysisResultModel(
            feedback_record_id=request.feedback_record.id,
            sensitivity_types=raw.sensitivity_types if raw else (),
            explanation=raw.explanation if raw else "No sensitive content detected.",
        )
