"""Tests for usage tracking + aggregation domain models.

Mirrors the production split between ``qfa.domain.models`` and
``qfa.domain.usage_models``: persistence primitives (``Operation``,
``CallStatus``, ``CallContext``, ``LLMCallRecord``) and aggregations
(``DistributionStats``, ``UsageMetrics``, ``OperationStats``,
``TenantStats``, ``TenantUsageStats``, ``OperationUsageStats``).
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from qfa.domain.usage_models import (
    CallContext,
    CallStatus,
    DistributionStats,
    LLMCallRecord,
    Operation,
    OperationStats,
    OperationUsageStats,
    TenantStats,
    TenantUsageStats,
    UsageMetrics,
)

# --- CallContext ---


class TestCallContext:
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


# --- TenantUsageStats v2 ---


def _zero_dist() -> DistributionStats:
    return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)


def _zero_tokens() -> DistributionStats:
    return _zero_dist()


class TestUsageStatsV2:
    def _metrics(self, total_calls: int = 1) -> UsageMetrics:
        return UsageMetrics(
            total_calls=total_calls,
            total_cost_usd=Decimal("0.5"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )

    def test_construct_with_llm_call_stats_and_operations(self):
        """TenantUsageStats requires ``llm_call_stats``; ``operations`` defaults to empty tuple.

        Guards the wire shape: every tenant block must carry the per-LLM-call
        view, and the absence of per-operation data is represented as ``()``
        — never ``None``.
        """
        stats = TenantUsageStats(
            tenant_id="t1",
            total_calls=10,
            failed_calls=1,
            total_cost_usd=Decimal("0.5"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._metrics(total_calls=42),
        )
        assert stats.operations == ()
        assert stats.llm_call_stats.total_calls == 42

    def test_requires_llm_call_stats(self):
        """``llm_call_stats`` is mandatory — omitting it must raise ValidationError.

        Prevents an empty per-LLM-call block from sneaking through and
        leaving clients to special-case None.
        """
        with pytest.raises(ValidationError):
            TenantUsageStats(  # type:ignore [ty:missing-argument]
                tenant_id="t1",
                total_calls=0,
                total_cost_usd=Decimal("0"),
                call_duration=_zero_dist(),
                input_tokens=_zero_tokens(),
                output_tokens=_zero_tokens(),
            )

    def test_operations_is_a_tuple(self):
        """``operations`` is a tuple per ADR-001 (frozen + tuples).

        Catches an accidental list typing — would break frozen invariants
        and dict-like access patterns downstream.
        """
        op = OperationStats(
            operation=Operation.ANALYZE,
            total_calls=1,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._metrics(),
        )
        stats = TenantUsageStats(
            tenant_id="t1",
            total_calls=1,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._metrics(),
            operations=(op,),
        )
        assert isinstance(stats.operations, tuple)
        assert stats.operations[0].operation == Operation.ANALYZE

    def test_tenant_id_none_allowed_for_grand_total(self):
        """tenant_id=None is allowed — the grand-total entry in /v1/usage/all.

        Guards the existing sentinel convention.
        """
        stats = TenantUsageStats(
            tenant_id=None,
            total_calls=0,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._metrics(total_calls=0),
        )
        assert stats.tenant_id is None


class TestUsageMetrics:
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

    def test_decimal_cost_serializes_as_float(self):
        """total_cost_usd serialises to a JSON-friendly float, not a Decimal.

        OpenAPI/JSON does not have native Decimal; this matches the existing
        TenantUsageStats serialisation and prevents quoting as a string.
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


class TestOperationStats:
    def _llm_metrics(self) -> UsageMetrics:
        return UsageMetrics(
            total_calls=3,
            total_cost_usd=Decimal("0.1"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )

    def test_inherits_metric_fields(self):
        """OperationStats carries all UsageMetrics fields plus ``operation`` + ``llm_call_stats``.

        Guarantees the per-operation block exposes the per-invocation view
        as its own attributes (not nested) — clients can read
        ``operations[i].total_cost_usd`` directly.
        """
        op = OperationStats(
            operation=Operation.ANALYZE,
            total_calls=1,
            total_cost_usd=Decimal("0.5"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
        )
        assert op.operation == Operation.ANALYZE
        assert op.total_calls == 1
        assert op.total_cost_usd == Decimal("0.5")
        assert op.llm_call_stats.total_calls == 3

    def test_requires_operation_and_llm_call_stats(self):
        """``operation`` and ``llm_call_stats`` are mandatory.

        Omitting them must raise — otherwise composition could silently
        forget to attach either, producing structurally invalid responses.
        """
        with pytest.raises(ValidationError):
            OperationStats(  # type:ignore [ty:missing-argument]
                total_calls=1,
                total_cost_usd=Decimal("0"),
                call_duration=_zero_dist(),
                input_tokens=_zero_tokens(),
                output_tokens=_zero_tokens(),
            )


class TestTenantStats:
    def _llm_metrics(self) -> UsageMetrics:
        return UsageMetrics(
            total_calls=2,
            total_cost_usd=Decimal("0.1"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )

    def test_inherits_metric_fields(self):
        """TenantStats carries all UsageMetrics fields plus ``tenant_id`` + ``llm_call_stats``.

        Pins the wire shape of the nested per-tenant block returned under
        each operation by /v1/usage/all/by-operation.
        """
        t = TenantStats(
            tenant_id="acme",
            total_calls=1,
            total_cost_usd=Decimal("0.5"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
        )
        assert t.tenant_id == "acme"
        assert t.total_calls == 1
        assert t.llm_call_stats.total_calls == 2

    def test_requires_tenant_id_and_llm_call_stats(self):
        """``tenant_id`` and ``llm_call_stats`` are mandatory.

        Tenant blocks must always carry their identifier and per-LLM-call
        view; defaults would mask composition bugs that drop the discriminator.
        """
        with pytest.raises(ValidationError):
            TenantStats(  # type:ignore [ty:missing-argument]
                total_calls=1,
                total_cost_usd=Decimal("0"),
                call_duration=_zero_dist(),
                input_tokens=_zero_tokens(),
                output_tokens=_zero_tokens(),
                llm_call_stats=self._llm_metrics(),
            )


class TestOperationUsageStats:
    def _llm_metrics(self) -> UsageMetrics:
        return UsageMetrics(
            total_calls=4,
            total_cost_usd=Decimal("0.2"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
        )

    def _tenant(self, tid: str = "acme") -> TenantStats:
        return TenantStats(
            tenant_id=tid,
            total_calls=1,
            total_cost_usd=Decimal("0.1"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
        )

    def test_construct_with_llm_call_stats_and_tenants(self):
        """OperationUsageStats requires ``llm_call_stats``; ``tenants`` defaults to empty tuple.

        Mirrors the TenantUsageStats invariant for the inverse hierarchy: every
        operation block must carry the per-LLM-call view, and the absence
        of per-tenant data is ``()`` — never ``None``.
        """
        stats = OperationUsageStats(
            operation=Operation.ANALYZE,
            total_calls=5,
            total_cost_usd=Decimal("1.0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
        )
        assert stats.operation == Operation.ANALYZE
        assert stats.tenants == ()
        assert stats.llm_call_stats.total_calls == 4

    def test_operation_none_allowed_for_grand_total(self):
        """operation=None is allowed — the grand-total entry of /v1/usage/all/by-operation.

        Parallels the tenant_id=None convention on TenantUsageStats.
        """
        stats = OperationUsageStats(
            operation=None,
            total_calls=0,
            total_cost_usd=Decimal("0"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
        )
        assert stats.operation is None

    def test_tenants_is_a_tuple(self):
        """``tenants`` is a tuple per ADR-001 (frozen + tuples).

        Catches an accidental list typing — would break frozen invariants
        and dict-like access patterns downstream.
        """
        stats = OperationUsageStats(
            operation=Operation.ANALYZE,
            total_calls=1,
            total_cost_usd=Decimal("0.1"),
            call_duration=_zero_dist(),
            input_tokens=_zero_tokens(),
            output_tokens=_zero_tokens(),
            llm_call_stats=self._llm_metrics(),
            tenants=(self._tenant("acme"), self._tenant("beta")),
        )
        assert isinstance(stats.tenants, tuple)
        assert stats.tenants[0].tenant_id == "acme"

    def test_requires_llm_call_stats(self):
        """``llm_call_stats`` is mandatory — omitting it must raise ValidationError.

        Prevents an operation block from being emitted without the
        per-LLM-call view, which clients rely on for fan-out math.
        """
        with pytest.raises(ValidationError):
            OperationUsageStats(  # type:ignore [ty:missing-argument]
                operation=Operation.ANALYZE,
                total_calls=0,
                total_cost_usd=Decimal("0"),
                call_duration=_zero_dist(),
                input_tokens=_zero_tokens(),
                output_tokens=_zero_tokens(),
            )
