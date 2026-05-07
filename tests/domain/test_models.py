"""Tests for domain models."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    CallContext,
    CallStatus,
    DistributionStats,
    FeedbackItemModel,
    LLMCallRecord,
    LLMResponse,
    Operation,
    TenantApiKey,
    TokenStats,
    UsageStats,
)

# --- FeedbackItemModel ---


class TestFeedbackItemModel:
    def test_construct_with_valid_data(self):
        doc = FeedbackItemModel(id="doc-1", text="Some feedback")
        assert doc.id == "doc-1"
        assert doc.text == "Some feedback"

    def test_metadata_defaults_to_empty_dict(self):
        doc = FeedbackItemModel(id="doc-1", text="Some feedback")
        assert doc.metadata == {}

    def test_metadata_with_values(self):
        meta = {"source": "email", "score": 5, "weight": 0.8, "urgent": True}
        doc = FeedbackItemModel(id="doc-1", text="feedback", metadata=meta)
        assert doc.metadata == meta

    def test_frozen_raises_on_assignment(self):
        doc = FeedbackItemModel(id="doc-1", text="feedback")
        with pytest.raises(ValidationError):
            doc.text = "changed"

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError):
            FeedbackItemModel(id="doc-1", text="")

    def test_text_exceeding_max_length_raises(self):
        with pytest.raises(ValidationError):
            FeedbackItemModel(id="doc-1", text="x" * 100_001)

    def test_text_at_max_length_is_valid(self):
        doc = FeedbackItemModel(id="doc-1", text="x" * 100_000)
        assert len(doc.text) == 100_000


# --- AnalysisRequest ---


class TestAnalysisRequestModel:
    def _make_doc(self, doc_id: str = "doc-1") -> FeedbackItemModel:
        return FeedbackItemModel(id=doc_id, text="feedback text")

    def test_construct_with_valid_data(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize feedback", tenant_id="tenant-1"
        )
        assert req.documents == (doc,)
        assert req.prompt == "Summarize feedback"
        assert req.tenant_id == "tenant-1"

    def test_documents_is_a_tuple(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        assert isinstance(req.documents, tuple)

    def test_frozen_raises_on_assignment(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            req.prompt = "changed"

    def test_empty_documents_raises(self):
        with pytest.raises(ValidationError):
            AnalysisRequestModel(documents=(), prompt="Summarize", tenant_id="tenant-1")

    def test_empty_prompt_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(documents=(doc,), prompt="", tenant_id="tenant-1")

    def test_prompt_exceeding_max_length_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                documents=(doc,), prompt="x" * 4001, tenant_id="tenant-1"
            )

    def test_prompt_at_max_length_is_valid(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            documents=(doc,), prompt="x" * 4000, tenant_id="tenant-1"
        )
        assert len(req.prompt) == 4000


# --- AnalysisResult ---


class TestAnalysisResultModel:
    def test_construct_with_valid_data(self):
        result = AnalysisResultModel(
            result="Summary text",
        )
        assert result.result == "Summary text"

    def test_frozen_raises_on_assignment(self):
        result = AnalysisResultModel(
            result="Summary",
        )
        with pytest.raises(ValidationError):
            result.result = "changed"


# --- LLMResponse ---


class TestLLMResponse:
    def test_construct_with_valid_data(self):
        resp = LLMResponse(
            structured="Generated text",
            model="gpt-4",
            prompt_tokens=80,
            completion_tokens=40,
            cost=0.001,
        )
        assert resp.structured == "Generated text"
        assert resp.model == "gpt-4"
        assert resp.prompt_tokens == 80
        assert resp.completion_tokens == 40

    def test_frozen_raises_on_assignment(self):
        resp = LLMResponse(
            structured="text",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.001,
        )
        with pytest.raises(ValidationError):
            resp.structured = "changed"


# --- TenantApiKey ---


class TestTenantApiKey:
    def test_construct_with_valid_data(self):
        key = TenantApiKey(
            key_id="tenant-1-0", name="prod-key", key="sk-abc123", tenant_id="tenant-1"
        )
        assert key.key_id == "tenant-1-0"
        assert key.name == "prod-key"
        assert key.key.get_secret_value() == "sk-abc123"
        assert key.tenant_id == "tenant-1"

    def test_frozen_raises_on_assignment(self):
        key = TenantApiKey(
            key_id="tenant-1-0", name="prod-key", key="sk-abc123", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            key.name = "changed"


# --- Operation / CallStatus / CallContext ---


class TestOperationEnum:
    def test_string_values(self):
        assert Operation.ANALYZE == "analyze"
        assert Operation.SUMMARIZE == "summarize"
        assert Operation.SUMMARIZE_AGGREGATE == "summarize_aggregate"
        assert Operation.ASSIGN_CODES == "assign_codes"
        assert Operation.UNKNOWN == "unknown"


class TestCallStatusEnum:
    def test_string_values(self):
        assert CallStatus.OK == "ok"
        assert CallStatus.ERROR == "error"


class TestCallContext:
    def test_construct(self):
        ctx = CallContext(tenant_id="t1", operation=Operation.ANALYZE)
        assert ctx.tenant_id == "t1"
        assert ctx.operation == Operation.ANALYZE

    def test_frozen(self):
        ctx = CallContext(tenant_id="t1", operation=Operation.ANALYZE)
        with pytest.raises(ValidationError):
            ctx.tenant_id = "t2"


# --- LLMCallRecord ---


def _now() -> datetime:
    return datetime.now(UTC)


class TestLLMCallRecord:
    def test_ok_status(self):
        rec = LLMCallRecord(
            tenant_id="t1",
            operation=Operation.ANALYZE,
            timestamp=_now(),
            call_duration_ms=100,
            model="gpt-4",
            input_tokens=10,
            output_tokens=20,
            cost_usd=Decimal("0.0001"),
            status=CallStatus.OK,
        )
        assert rec.status == CallStatus.OK
        assert rec.error_class is None

    def test_error_status_requires_error_class(self):
        with pytest.raises(ValidationError):
            LLMCallRecord(
                tenant_id="t1",
                operation=Operation.ANALYZE,
                timestamp=_now(),
                call_duration_ms=100,
                model="",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                status=CallStatus.ERROR,
                error_class=None,
            )

    def test_ok_status_rejects_error_class(self):
        with pytest.raises(ValidationError):
            LLMCallRecord(
                tenant_id="t1",
                operation=Operation.ANALYZE,
                timestamp=_now(),
                call_duration_ms=100,
                model="gpt-4",
                input_tokens=10,
                output_tokens=20,
                cost_usd=Decimal("0.0001"),
                status=CallStatus.OK,
                error_class="LLMTimeoutError",
            )


# --- UsageStats new fields ---


class TestUsageStatsExtensions:
    def test_has_failed_calls_and_total_cost(self):
        stats = UsageStats(
            tenant_id="t1",
            total_calls=10,
            failed_calls=1,
            total_cost_usd=Decimal("0.5"),
            call_duration=DistributionStats(avg=1, min=0, max=2, p5=0, p95=2),
            input_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
            output_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
        )
        assert stats.total_cost_usd == Decimal("0.5")
        assert stats.failed_calls == 1
