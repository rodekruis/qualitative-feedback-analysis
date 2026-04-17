"""Tests for the TrackingOrchestrator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from qfa.domain import FeedbackItem
from qfa.domain.models import AnalysisRequest, AnalysisResult
from qfa.services.tracking_orchestrator import TrackingOrchestrator

pytestmark = pytest.mark.asyncio


def _make_request(tenant_id: str = "tenant-1") -> AnalysisRequest:
    return AnalysisRequest(
        documents=(FeedbackItem(id="d1", text="Some feedback text"),),
        prompt="Summarize",
        tenant_id=tenant_id,
    )


def _make_result() -> AnalysisResult:
    return AnalysisResult(
        result="Analysis output",
        model="gpt-4-test",
        prompt_tokens=10,
        completion_tokens=20,
        cost=0.001,
    )


async def test_delegates_to_inner_orchestrator():
    inner = AsyncMock()
    expected = _make_result()
    inner.analyze.return_value = expected
    usage_repo = AsyncMock()

    tracking = TrackingOrchestrator(inner=inner, usage_repo=usage_repo)
    result = await tracking.analyze(
        _make_request(), datetime.now(UTC) + timedelta(seconds=60)
    )

    assert result is expected
    inner.analyze.assert_awaited_once()


async def test_records_call_after_analysis():
    inner = AsyncMock()
    inner.analyze.return_value = _make_result()
    usage_repo = AsyncMock()

    tracking = TrackingOrchestrator(inner=inner, usage_repo=usage_repo)
    await tracking.analyze(
        _make_request(tenant_id="t1"), datetime.now(UTC) + timedelta(seconds=60)
    )

    usage_repo.record_call.assert_awaited_once()
    record = usage_repo.record_call.call_args[0][0]
    assert record.tenant_id == "t1"
    assert record.model == "gpt-4-test"
    assert record.input_tokens == 10
    assert record.output_tokens == 20
    assert record.call_duration_ms >= 0


async def test_recording_failure_does_not_break_analysis():
    inner = AsyncMock()
    inner.analyze.return_value = _make_result()
    usage_repo = AsyncMock()
    usage_repo.record_call.side_effect = RuntimeError("DB down")

    tracking = TrackingOrchestrator(inner=inner, usage_repo=usage_repo)
    result = await tracking.analyze(
        _make_request(), datetime.now(UTC) + timedelta(seconds=60)
    )

    assert result.result == "Analysis output"


async def test_propagates_analysis_errors():
    inner = AsyncMock()
    inner.analyze.side_effect = ValueError("bad request")
    usage_repo = AsyncMock()

    tracking = TrackingOrchestrator(inner=inner, usage_repo=usage_repo)
    with pytest.raises(ValueError, match="bad request"):
        await tracking.analyze(
            _make_request(), datetime.now(UTC) + timedelta(seconds=60)
        )

    usage_repo.record_call.assert_not_awaited()
