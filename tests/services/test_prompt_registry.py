"""``SYSTEM_PROMPTS`` matches the text every real flow actually sends (review on #405).

Each of the 11 flows is driven once, over a fake LLM, with a
``prompt_versions`` map covering every name in ``SYSTEM_PROMPTS`` — so every
call records a ``PromptRef``. For each recorded call, the system message
sent is checked against the registry entry for that name. Without this,
a call site that stopped building its message through the shared
constant/builder the registry also reads would only surface as a silently
wrong Langfuse prompt version, never a test failure.

Per ADR-017 every service here is built over the *real*
:class:`~qfa.services.llm_call_executor.LLMCallExecutor`, over the fake
driven adapters the other service-test modules already ship — there is no
fake executor and no second set of doubles.
"""

from datetime import UTC, datetime, timedelta

import pytest

from qfa.domain.models import (
    AggregateSummaryResultModel,
    AnalysisRequestModel,
    CodingAssignmentRequestModel,
    CodingFramework,
    CodingNode,
    CommunityMeetingRecordModel,
    CommunityMeetingRecordSummaryModel,
    FeedbackRecordMetadataModel,
    FeedbackRecordModel,
    FeedbackRecordSummaryModel,
    LLMResponse,
    PromptRef,
    SensitivityAnalysisRequestModel,
    SensitivityClassificationModel,
    SensitivityClassificationModelList,
    SingleSummaryCommunityMeetingRequestModel,
    SingleSummaryRequestModel,
    SummaryCommunityMeetingResultModel,
    SummaryRequestModel,
    SummaryResultModel,
)
from qfa.domain.sensitivity_types import SensitivityType
from qfa.services.analyze import AnalyzeService
from qfa.services.coding import CodingService
from qfa.services.coding_classifier import CodingResponse
from qfa.services.llm_call_executor import LLMCallExecutor
from qfa.services.prompt_registry import SYSTEM_PROMPTS
from qfa.services.sensitivity import SensitivityService
from qfa.services.summarize import SummarizeService
from qfa.settings import AnalyzeSettings, OrchestratorSettings

from .test_analyze_hierarchical import FakeEmbeddingPort, RecordingAnonymizer, _records
from .test_analyze_hierarchical import RecordingLLM as _HierarchicalLLM
from .test_summarize import FakeAnonymizer, FakeLLMPort

TENANT_ID = "tenant-42"
LLM_TIMEOUT = 30.0
MAX_TOKENS = 100_000

#: Prompt names filled via ``.format()`` before being sent: the live
#: system message therefore never starts with the raw (unfilled) template
#: text the registry holds, so these are checked for presence only.
_TEMPLATES = {"analyze-judge", "summarize-judge"}

#: One Langfuse version per name, so every flow below tags its calls with a
#: ``PromptRef`` rather than ``None``.
_PROMPT_VERSIONS = dict.fromkeys(SYSTEM_PROMPTS, 1)


def _future_deadline(seconds=300):
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def _feedback_record(content="Some feedback text."):
    return FeedbackRecordModel(
        id="doc-1",
        content=content,
        metadata=FeedbackRecordMetadataModel(),
    )


def _executor(llm, anonymizer):
    return LLMCallExecutor(
        llm=llm,
        anonymizer=anonymizer,
        settings=OrchestratorSettings(),
        llm_timeout_seconds=LLM_TIMEOUT,
        max_total_tokens=MAX_TOKENS,
    )


def _analyze_judge_text():
    """A valid free-text reply for ``ANALYZE_JUDGE_PROMPT``/``_JUDGE_PROMPT`` (shared format)."""
    return (
        "FAITHFULNESS: 0.9\nCOVERAGE: 0.8\nCLARITY: 0.9\n"
        "UNCERTAINTY_EXPLANATION: looks fine"
    )


def _llm_response(structured):
    return LLMResponse(
        structured=structured,
        model="fake",
        prompt_tokens=1,
        completion_tokens=1,
        cost=0.0,
    )


async def _single_pass_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``analyze_bulk`` (single_pass): tags ``analyze-single-pass-system``/``analyze-judge``."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response("analysis text"),
            _llm_response(_analyze_judge_text()),
        ]
    )
    service = AnalyzeService(
        executor=_executor(fake_llm, FakeAnonymizer()),
        llm=fake_llm,
        anonymizer=FakeAnonymizer(),
        settings=OrchestratorSettings(),
        max_total_tokens=MAX_TOKENS,
        prompt_versions=_PROMPT_VERSIONS,
    )
    request = AnalysisRequestModel(
        feedback_records=(_feedback_record(),), prompt="trends?", tenant_id=TENANT_ID
    )

    await service.analyze_bulk(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


async def _hierarchical_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``analyze_hierarchical``: tags the map/reduce/judge names."""
    llm = _HierarchicalLLM()
    executor = _executor(llm, RecordingAnonymizer())
    service = AnalyzeService(
        executor=executor,
        llm=llm,
        anonymizer=RecordingAnonymizer(),
        embedder=FakeEmbeddingPort(),
        settings=OrchestratorSettings(),
        analyze_settings=AnalyzeSettings(min_cluster_size=2),
        max_total_tokens=MAX_TOKENS,
        prompt_versions=_PROMPT_VERSIONS,
    )
    water = _records(4, "water access was limited " * 5, "w")
    health = _records(4, "health clinic medicine " * 5, "h")
    request = AnalysisRequestModel(
        feedback_records=water + health, prompt="trends?", tenant_id=TENANT_ID
    )

    await service.analyze_hierarchical(request, _future_deadline(120), anonymize=True)

    return [
        (prompt, system_message)
        for prompt, (system_message, _user_message, _response_model) in zip(
            llm.prompt_calls, llm.calls, strict=True
        )
    ]


async def _summarize_bulk_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``summarize_bulk``: tags ``summarize-aggregate-system``/``summarize-judge``."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response(
                AggregateSummaryResultModel(
                    title="t", summary="- point", quality_score=0.0
                )
            ),
            _llm_response(_analyze_judge_text()),
        ]
    )
    service = SummarizeService(
        llm=fake_llm,
        anonymizer=FakeAnonymizer(),
        executor=_executor(fake_llm, FakeAnonymizer()),
        prompt_versions=_PROMPT_VERSIONS,
    )
    request = SummaryRequestModel(
        feedback_records=(_feedback_record(),), tenant_id=TENANT_ID
    )

    await service.summarize_bulk(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


async def _summarize_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``summarize``: tags ``summarize-single-system``/``summarize-judge``."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response(
                SummaryResultModel(
                    feedback_record_summaries=(
                        FeedbackRecordSummaryModel(
                            id="doc-1", title="t", summary="- point", quality_score=0.0
                        ),
                    )
                )
            ),
            _llm_response(_analyze_judge_text()),
        ]
    )
    service = SummarizeService(
        llm=fake_llm,
        anonymizer=FakeAnonymizer(),
        executor=_executor(fake_llm, FakeAnonymizer()),
        prompt_versions=_PROMPT_VERSIONS,
    )
    request = SingleSummaryRequestModel(
        feedback_record=_feedback_record(), tenant_id=TENANT_ID
    )

    await service.summarize(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


async def _summarize_community_meeting_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``summarize_community_meeting``: tags its own system/judge names."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response(
                SummaryCommunityMeetingResultModel(
                    community_meeting_record_summaries=(
                        CommunityMeetingRecordSummaryModel(
                            id="meeting-1",
                            title="t",
                            summary="- point",
                            quality_score=0.0,
                        ),
                    )
                )
            ),
            _llm_response(_analyze_judge_text()),
        ]
    )
    service = SummarizeService(
        llm=fake_llm,
        anonymizer=FakeAnonymizer(),
        executor=_executor(fake_llm, FakeAnonymizer()),
        prompt_versions=_PROMPT_VERSIONS,
    )
    request = SingleSummaryCommunityMeetingRequestModel(
        community_meeting_record=CommunityMeetingRecordModel(
            id="meeting-1", meetingNotes="The community asked for safer water."
        ),
        tenant_id=TENANT_ID,
    )

    await service.summarize_community_meeting(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


async def _assign_codes_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive ``assign_codes``: tags ``coding-classifier-system``/``coding-classifier-judge``."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response(CodingResponse(selected=[0])),
            _llm_response("SCORE: 0.9\nEXPLANATION: fits"),
        ]
    )
    service = CodingService(
        llm=fake_llm,
        anonymizer=FakeAnonymizer(),
        executor=_executor(fake_llm, FakeAnonymizer()),
        prompt_versions=_PROMPT_VERSIONS,
    )
    request = CodingAssignmentRequestModel(
        feedback_record=_feedback_record(),
        coding_levels=CodingFramework(
            root_codes=[CodingNode(id="code-1", name="Code A")]
        ),
        max_codes=5,
        tenant_id=TENANT_ID,
    )

    await service.assign_codes(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


async def _detect_sensitive_content_calls() -> list[tuple[PromptRef | None, str]]:
    """Drive it: tags ``sensitivity-detection-system``/``sensitivity-detection-judge``."""
    fake_llm = FakeLLMPort(
        responses=[
            _llm_response(
                SensitivityClassificationModelList(
                    results=(
                        SensitivityClassificationModel(
                            feedback_record_id="doc-1",
                            sensitivity_types=(SensitivityType.CORRUPTION,),
                            explanation="Alleges bribery.",
                        ),
                    )
                )
            ),
            _llm_response("SCORE: 0.9\nEXPLANATION: correct"),
        ]
    )
    service = SensitivityService(
        executor=_executor(fake_llm, FakeAnonymizer()), prompt_versions=_PROMPT_VERSIONS
    )
    request = SensitivityAnalysisRequestModel(
        feedback_record=_feedback_record(), tenant_id=TENANT_ID
    )

    await service.detect_sensitive_content(request, _future_deadline())

    return [(call["prompt"], call["system_message"]) for call in fake_llm.calls]


@pytest.mark.asyncio
async def test_every_flow_sends_the_text_the_registry_holds():
    """Every tagged call's system message matches (or starts with) its registry entry.

    A regression here — a call site drifting from the shared constant or
    builder ``SYSTEM_PROMPTS`` also reads — would otherwise only show up as
    a wrong prompt version mirrored to Langfuse, never a failing test.
    """
    calls = [
        *await _single_pass_calls(),
        *await _hierarchical_calls(),
        *await _summarize_bulk_calls(),
        *await _summarize_calls(),
        *await _summarize_community_meeting_calls(),
        *await _assign_codes_calls(),
        *await _detect_sensitive_content_calls(),
    ]

    assert calls, "expected at least one tagged call"
    seen_names: set[str] = set()
    for prompt, system_message in calls:
        assert prompt is not None, "every flow here was given a version for every name"
        assert prompt.name in SYSTEM_PROMPTS
        if prompt.name not in _TEMPLATES:
            assert system_message.startswith(SYSTEM_PROMPTS[prompt.name])
        seen_names.add(prompt.name)

    assert seen_names == set(SYSTEM_PROMPTS), (
        "every registry name should be exercised here"
    )
