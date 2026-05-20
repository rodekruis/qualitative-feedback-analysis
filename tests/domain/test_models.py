"""Tests for domain models."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

# --- UsageMetrics / OperationStats / UsageStats v2 ---
from qfa.domain.models import (
    AnalysisRequestModel,
    AnalysisResultModel,
    CallContext,
    CallStatus,
    DistributionStats,
    FeedbackRecordModel,
    LLMCallRecord,
    LLMResponse,
    Operation,
    TenantApiKey,
    TokenStats,
    UsageMetrics,
    UsageStats,
)


def _zero_dist() -> DistributionStats:
    return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0)


def _zero_tokens() -> TokenStats:
    return TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)


# --- FeedbackRecordModel ---


class TestFeedbackRecordModel:
    def test_construct_with_valid_data(self):
        doc = FeedbackRecordModel(id="doc-1", text="Some feedback")
        assert doc.id == "doc-1"
        assert doc.text == "Some feedback"

    def test_metadata_defaults_to_empty_dict(self):
        doc = FeedbackRecordModel(id="doc-1", text="Some feedback")
        assert doc.metadata == {}

    def test_metadata_with_values(self):
        meta = {"source": "email", "score": 5, "weight": 0.8, "urgent": True}
        doc = FeedbackRecordModel(id="doc-1", text="feedback", metadata=meta)
        assert doc.metadata == meta

    def test_frozen_raises_on_assignment(self):
        doc = FeedbackRecordModel(id="doc-1", text="feedback")
        with pytest.raises(ValidationError):
            doc.text = "changed"

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError):
            FeedbackRecordModel(id="doc-1", text="")

    def test_text_exceeding_max_length_raises(self):
        with pytest.raises(ValidationError):
            FeedbackRecordModel(id="doc-1", text="x" * 100_001)

    def test_text_at_max_length_is_valid(self):
        doc = FeedbackRecordModel(id="doc-1", text="x" * 100_000)
        assert len(doc.text) == 100_000


# --- AnalysisRequest ---


class TestAnalysisRequestModel:
    def _make_doc(self, doc_id: str = "doc-1") -> FeedbackRecordModel:
        return FeedbackRecordModel(id=doc_id, text="feedback text")

    def test_construct_with_valid_data(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize feedback", tenant_id="tenant-1"
        )
        assert req.feedback_records == (doc,)
        assert req.prompt == "Summarize feedback"
        assert req.tenant_id == "tenant-1"

    def test_feedback_records_is_a_tuple(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        assert isinstance(req.feedback_records, tuple)

    def test_frozen_raises_on_assignment(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="Summarize", tenant_id="tenant-1"
        )
        with pytest.raises(ValidationError):
            req.prompt = "changed"

    def test_empty_feedback_records_raises(self):
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(), prompt="Summarize", tenant_id="tenant-1"
            )

    def test_empty_prompt_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(doc,), prompt="", tenant_id="tenant-1"
            )

    def test_prompt_exceeding_max_length_raises(self):
        doc = self._make_doc()
        with pytest.raises(ValidationError):
            AnalysisRequestModel(
                feedback_records=(doc,), prompt="x" * 4001, tenant_id="tenant-1"
            )

    def test_prompt_at_max_length_is_valid(self):
        doc = self._make_doc()
        req = AnalysisRequestModel(
            feedback_records=(doc,), prompt="x" * 4000, tenant_id="tenant-1"
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
            key_id="tenant-1-0",
            name="prod-key",
            key="sk-abc123",  # type:ignore [ty:invalid-argument-type]
            tenant_id="tenant-1",
        )
        assert key.key_id == "tenant-1-0"
        assert key.name == "prod-key"
        assert key.key.get_secret_value() == "sk-abc123"
        assert key.tenant_id == "tenant-1"

    def test_frozen_raises_on_assignment(self):
        key = TenantApiKey(
            key_id="tenant-1-0",
            name="prod-key",
            key="sk-abc123",  # type:ignore [ty:invalid-argument-type]
            tenant_id="tenant-1",
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
        """Constructor accepts and exposes the three required fields.

        Smoke test for the data shape — guards against accidental rename
        or removal of any of ``tenant_id``, ``operation``, ``call_id``.
        """
        cid = uuid4()
        ctx = CallContext(tenant_id="t1", operation=Operation.ANALYZE, call_id=cid)
        assert ctx.tenant_id == "t1"
        assert ctx.operation == Operation.ANALYZE
        assert ctx.call_id == cid

    def test_frozen(self):
        """``CallContext`` is frozen — assignment after construction must raise.

        Immutability matters because the context lives in a ContextVar and
        gets snapshotted across asyncio tasks; mutating it mid-request
        would silently desynchronise tasks that already captured it.
        """
        ctx = CallContext(tenant_id="t1", operation=Operation.ANALYZE, call_id=uuid4())
        with pytest.raises(ValidationError):
            ctx.tenant_id = "t2"

    def test_call_id_required(self):
        """``call_id`` is mandatory — omitting it must raise ValidationError.

        Prevents accidental "context without correlation" — every record
        persisted through ``TrackingLLMAdapter`` depends on this field.
        """
        with pytest.raises(ValidationError):
            CallContext(tenant_id="t1", operation=Operation.ANALYZE)  # type:ignore [ty:missing-argument]


# --- LLMCallRecord ---


def _now() -> datetime:
    return datetime.now(UTC)


class TestLLMCallRecord:
    def test_ok_status(self):
        """A fully-populated ``status=OK`` record constructs cleanly.

        Covers the happy path: all required fields including ``call_id``
        accepted, ``error_class`` defaults to None, ``call_id`` is a UUID.
        """
        rec = LLMCallRecord(
            tenant_id="t1",
            operation=Operation.ANALYZE,
            call_id=uuid4(),
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
        assert isinstance(rec.call_id, UUID)

    def test_error_status_requires_error_class(self):
        """``status=ERROR`` with ``error_class=None`` must be rejected.

        The model validator enforces the ``error_class iff error`` invariant
        so the DB check-constraint and the application stay in agreement.
        """
        with pytest.raises(ValidationError):
            LLMCallRecord(
                tenant_id="t1",
                operation=Operation.ANALYZE,
                call_id=uuid4(),
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
        """``status=OK`` with a non-None ``error_class`` must be rejected.

        The other half of the ``error_class iff error`` invariant — keeps
        success rows from carrying stale error metadata.
        """
        with pytest.raises(ValidationError):
            LLMCallRecord(
                tenant_id="t1",
                operation=Operation.ANALYZE,
                call_id=uuid4(),
                timestamp=_now(),
                call_duration_ms=100,
                model="gpt-4",
                input_tokens=10,
                output_tokens=20,
                cost_usd=Decimal("0.0001"),
                status=CallStatus.OK,
                error_class="LLMTimeoutError",
            )

    def test_call_id_required(self):
        """``call_id`` is mandatory — omitting it must raise ValidationError.

        Mirror of the ``CallContext`` check at the record level; a record
        without ``call_id`` would defeat per-invocation aggregation.
        """
        with pytest.raises(ValidationError):
            LLMCallRecord(  # type:ignore [ty:missing-argument]
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


class TestUsageMetrics:
    def test_construct_with_all_fields(self):
        """A fully-populated UsageMetrics constructs and exposes all metric fields.

        Pins the field set used by both per-invocation (UsageStats /
        OperationStats inherit it) and per-LLM-call (llm_call_stats) views.
        """
        m = UsageMetrics(
            total_calls=10,
            failed_calls=2,
            total_cost_usd=Decimal("0.25"),
            call_duration=DistributionStats(avg=1, min=0, max=2, p5=0, p95=2),
            input_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
            output_tokens=TokenStats(avg=1, min=0, max=2, p5=0, p95=2, total=10),
        )
        assert m.total_calls == 10
        assert m.failed_calls == 2
        assert m.total_cost_usd == Decimal("0.25")

    def test_failed_calls_defaults_to_zero(self):
        """failed_calls defaults to 0 so tenants with no failures need not pass it."""
        m = UsageMetrics(
            total_calls=1,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )
        assert m.failed_calls == 0

    def test_frozen(self):
        """UsageMetrics is frozen — reassignment must raise.

        Aligns with ADR-001; mutation would break the response wire copy.
        """
        m = UsageMetrics(
            total_calls=1,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )
        with pytest.raises(ValidationError):
            m.total_calls = 99

    def test_decimal_cost_serializes_as_float(self):
        """total_cost_usd serialises to a JSON-friendly float, not a Decimal.

        OpenAPI/JSON does not have native Decimal; this matches the existing
        UsageStats serialisation and prevents quoting as a string.
        """
        m = UsageMetrics(
            total_calls=1,
            total_cost_usd=Decimal("0.5"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )
        dumped = m.model_dump()
        assert dumped["total_cost_usd"] == 0.5
