"""Tests for :class:`~qfa.services.sensitivity.SensitivityService`.

Moved here from ``test_orchestrator`` when the use case was extracted
(#263). Per ADR-017 the service is exercised over the **real**
:class:`~qfa.services.llm_call_executor.LLMCallExecutor`, constructed over
the existing ``FakeLLMPort`` / ``FakeAnonymizer`` doubles — there is no
fake executor and no stub of the service itself.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import uuid4

import pytest

from qfa.domain.errors import LLMError
from qfa.domain.models import (
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
    PromptRef,
    SensitivityAnalysisRequestModel,
    SensitivityClassificationModel,
    SensitivityClassificationModelList,
)
from qfa.domain.ports import EvaluationPort
from qfa.domain.sensitivity_types import SensitivityType
from qfa.services.call_context import Operation, call_scope
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.sensitivity import SensitivityService
from qfa.settings import OrchestratorSettings

# Reuse the doubles the summarize suite already ships rather than growing a
# second, drifting pair (ADR-017).
from .test_summarize import FakeAnonymizer, FakeLLMPort

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 10_000


class _RecordingEvaluator(EvaluationPort):
    """Captures ``(name, value)`` per recorded score instead of calling Langfuse."""

    def __init__(self):
        self.scores: list[tuple[str, float]] = []

    def record_score(self, *, trace_id: str, name: str, value: float) -> None:
        self.scores.append((name, value))


@pytest.fixture
def settings():
    return OrchestratorSettings()


def _make_feedback_record(doc_id="doc-1", content="Some feedback text."):
    return FeedbackRecordModel(
        id=doc_id,
        content=content,
        metadata=FeedbackRecordMetadataModel.model_validate({}),
        url_id="",
    )


def _make_llm_response(structured, model="gpt-4", cost=0.001):
    return LLMResponse(
        structured=structured,
        model=model,
        prompt_tokens=100,
        completion_tokens=50,
        cost=cost,
    )


def _make_sensitivity_request(feedback_record=None, tenant_id=TENANT_ID):
    if feedback_record is None:
        feedback_record = _make_feedback_record()
    return SensitivityAnalysisRequestModel(
        feedback_record=feedback_record,
        tenant_id=tenant_id,
    )


def _make_sensitivity_result(item_id="doc-1", sensitivity_types=None):
    if sensitivity_types is None:
        sensitivity_types = (SensitivityType.CORRUPTION,)
    return SensitivityClassificationModelList(
        results=(
            SensitivityClassificationModel(
                feedback_record_id=item_id,
                sensitivity_types=sensitivity_types,
                explanation="Contains a corruption allegation.",
            ),
        )
    )


def _future_deadline(seconds=300):
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def _judge_reply(score=0.9, explanation="The classification fits the record."):
    """A judge response in the two-line format ``_JUDGE_PROMPT`` asks for."""
    return _make_llm_response(structured=f"SCORE: {score}\nEXPLANATION: {explanation}")


def _make_service(
    fake_llm,
    settings,
    anonymizer=None,
    prompt_versions=None,
    judge_llm=None,
    evaluator=None,
):
    """Build the service over the real executor, as ADR-017 prescribes."""
    return SensitivityService(
        executor=LLMCallExecutor(
            llm=fake_llm,
            anonymizer=anonymizer or FakeAnonymizer(),
            settings=settings,
            llm_timeout_seconds=LLM_TIMEOUT,
            max_total_tokens=MAX_TOKENS,
        ),
        judge_llm=judge_llm,
        evaluator=evaluator,
        prompt_versions=prompt_versions,
    )


class TestDetectSensitiveContent:
    @pytest.mark.asyncio
    async def test_returns_structured_result_from_llm(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(
                    structured=_make_sensitivity_result(),
                )
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.feedback_record_id == "doc-1"
        assert result.is_sensitive is True
        assert fake_llm.calls[0]["response_model"] is SensitivityClassificationModelList

    @pytest.mark.asyncio
    async def test_tenant_id_in_llm_call(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
            ]
        )
        service = _make_service(fake_llm, settings)

        await service.detect_sensitive_content(
            _make_sensitivity_request(tenant_id="special-tenant"),
            _future_deadline(),
        )

        assert fake_llm.calls[0]["tenant_id"] == "special-tenant"

    @pytest.mark.asyncio
    async def test_result_id_is_pinned_to_request_record(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(
                    structured=SensitivityClassificationModelList(
                        results=(
                            SensitivityClassificationModel(
                                feedback_record_id="wrong-1",
                                sensitivity_types=(SensitivityType.CORRUPTION,),
                                explanation="Bribery risk.",
                            ),
                        )
                    ),
                ),
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(
                feedback_record=_make_feedback_record(doc_id="doc-1")
            ),
            _future_deadline(),
        )

        assert result.feedback_record_id == "doc-1"
        assert result.sensitivity_types == (SensitivityType.CORRUPTION,)

    @pytest.mark.asyncio
    async def test_prompt_contains_sensitivity_guidance(self, settings):
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())]
        )
        service = _make_service(fake_llm, settings)

        await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        system_msg = fake_llm.calls[0]["system_message"]
        assert "CORRUPTION: Apply when feedback alleges bribery" in system_msg


class TestSensitivityPromptVersions:
    """The one generation call tags its own prompt name and version (#398)."""

    _PROMPT_VERSIONS: ClassVar[dict[str, int]] = {"sensitivity-detection-system": 7}

    @pytest.mark.asyncio
    async def test_detect_call_tags_its_prompt(self, settings):
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())]
        )
        service = _make_service(
            fake_llm, settings, prompt_versions=self._PROMPT_VERSIONS
        )

        await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert fake_llm.calls[0]["prompt"] == PromptRef(
            name="sensitivity-detection-system", version=7
        )

    @pytest.mark.asyncio
    async def test_missing_version_leaves_the_call_untagged(self, settings):
        """``PromptRef`` requires both fields, so a missing version tags neither."""
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())]
        )
        service = _make_service(fake_llm, settings)

        await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert fake_llm.calls[0]["prompt"] is None


class TestJudgeCall:
    """The second call that scores the classification (``confidence``)."""

    @pytest.mark.asyncio
    async def test_confidence_comes_from_the_judge_reply(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
                _judge_reply(score=0.82),
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.confidence == 0.82
        assert len(fake_llm.calls) == 2

    @pytest.mark.asyncio
    async def test_judge_also_runs_on_a_record_rated_not_sensitive(self, settings):
        """The point of judging everything: a missed sensitive record is the costly error."""
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(
                    structured=_make_sensitivity_result(sensitivity_types=())
                ),
                _judge_reply(score=0.95),
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.is_sensitive is False
        assert result.confidence == 0.95
        assert len(fake_llm.calls) == 2

    @pytest.mark.asyncio
    async def test_judge_sees_the_classification_it_is_scoring(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
                _judge_reply(),
            ]
        )
        service = _make_service(fake_llm, settings)

        await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        judge_user_message = fake_llm.calls[1]["user_message"]
        assert SensitivityType.CORRUPTION.value in judge_user_message
        assert "Contains a corruption allegation." in judge_user_message

    @pytest.mark.asyncio
    async def test_not_sensitive_is_labelled_rather_than_left_blank(self, settings):
        """An empty type list must read as a decision, not as a missing field."""
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(
                    structured=_make_sensitivity_result(sensitivity_types=())
                ),
                _judge_reply(),
            ]
        )
        service = _make_service(fake_llm, settings)

        await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert "none (not sensitive)" in fake_llm.calls[1]["user_message"]

    @pytest.mark.asyncio
    async def test_failed_judge_call_leaves_the_classification_intact(self, settings):
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())],
            errors=[None, LLMError("judge is down")],
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.is_sensitive is True
        assert result.explanation == "Contains a corruption allegation."
        assert result.confidence is None

    @pytest.mark.asyncio
    async def test_unparsable_judge_reply_yields_no_confidence(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
                _make_llm_response(structured="I think it is probably fine."),
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.confidence is None

    @pytest.mark.asyncio
    async def test_out_of_range_judge_score_yields_no_confidence(self, settings):
        """Rejected here rather than by the model, which would raise on ``ge``/``le``."""
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
                _judge_reply(score=1.4),
            ]
        )
        service = _make_service(fake_llm, settings)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert result.confidence is None

    @pytest.mark.asyncio
    async def test_judge_call_goes_to_the_judge_connection_when_given(self, settings):
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())]
        )
        fake_judge_llm = FakeLLMPort(responses=[_judge_reply(score=0.6)])
        service = _make_service(fake_llm, settings, judge_llm=fake_judge_llm)

        result = await service.detect_sensitive_content(
            _make_sensitivity_request(), _future_deadline()
        )

        assert len(fake_llm.calls) == 1
        assert len(fake_judge_llm.calls) == 1
        assert result.confidence == 0.6

    @pytest.mark.asyncio
    async def test_confidence_is_recorded_as_a_live_score(self, settings):
        fake_llm = FakeLLMPort(
            responses=[
                _make_llm_response(structured=_make_sensitivity_result()),
                _judge_reply(score=0.77),
            ]
        )
        evaluator = _RecordingEvaluator()
        service = _make_service(fake_llm, settings, evaluator=evaluator)

        async with call_scope(TENANT_ID, Operation.DETECT_SENSITIVE, uuid4()):
            await service.detect_sensitive_content(
                _make_sensitivity_request(), _future_deadline()
            )

        assert evaluator.scores == [("sensitivity_confidence", 0.77)]

    @pytest.mark.asyncio
    async def test_no_live_score_when_the_judge_failed(self, settings):
        fake_llm = FakeLLMPort(
            responses=[_make_llm_response(structured=_make_sensitivity_result())],
            errors=[None, LLMError("judge is down")],
        )
        evaluator = _RecordingEvaluator()
        service = _make_service(fake_llm, settings, evaluator=evaluator)

        async with call_scope(TENANT_ID, Operation.DETECT_SENSITIVE, uuid4()):
            await service.detect_sensitive_content(
                _make_sensitivity_request(), _future_deadline()
            )

        assert evaluator.scores == []
