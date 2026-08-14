"""Tests for :class:`~qfa.services.sensitivity.SensitivityService`.

Moved here from ``test_orchestrator`` when the use case was extracted
(#263). Per ADR-017 the service is exercised over the **real**
:class:`~qfa.services.llm_call_executor.LLMCallExecutor`, constructed over
the existing ``FakeLLMPort`` / ``FakeAnonymizer`` doubles — there is no
fake executor and no stub of the service itself.
"""

from datetime import UTC, datetime, timedelta

import pytest

from qfa.domain.models import (
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    LLMResponse,
    SensitivityAnalysisRequestModel,
    SensitivityAnalysisResultModel,
    SensitivityAnalysisResultModelList,
)
from qfa.domain.sensitivity_types import SensitivityType
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.sensitivity import SensitivityService
from qfa.settings import OrchestratorSettings

# Reuse the doubles the orchestrator suite already ships rather than growing a
# second, drifting pair (ADR-017).
from .test_orchestrator import FakeAnonymizer, FakeLLMPort

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 10_000


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
    return SensitivityAnalysisResultModelList(
        results=(
            SensitivityAnalysisResultModel(
                feedback_record_id=item_id,
                sensitivity_types=sensitivity_types,
                explanation="Contains a corruption allegation.",
            ),
        )
    )


def _future_deadline(seconds=300):
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def _make_service(fake_llm, settings, anonymizer=None):
    """Build the service over the real executor, as ADR-017 prescribes."""
    return SensitivityService(
        executor=LLMCallExecutor(
            llm=fake_llm,
            anonymizer=anonymizer or FakeAnonymizer(),
            settings=settings,
            llm_timeout_seconds=LLM_TIMEOUT,
            max_total_tokens=MAX_TOKENS,
        )
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
        assert fake_llm.calls[0]["response_model"] is SensitivityAnalysisResultModelList

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
                    structured=SensitivityAnalysisResultModelList(
                        results=(
                            SensitivityAnalysisResultModel(
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
