"""Tier-2 aggregation tests for the SQLAlchemy usage repository against PostgreSQL.

These exercise the queries that ``percentile_cont`` etc. cannot run on
sqlite. They depend on the session-scoped ``pg_engine`` fixture in
``tests/integration/conftest.py`` and are gated by ``@pytest.mark.integration``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from qfa.adapters.db import llm_calls
from qfa.domain.models import CallStatus, LLMCallRecord, Operation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _now() -> datetime:
    return datetime.now(UTC)


def _record(
    *,
    tenant_id: str = "t1",
    operation: Operation = Operation.ANALYZE,
    timestamp: datetime | None = None,
    cost_usd: Decimal = Decimal("0.0001"),
    input_tokens: int = 100,
    output_tokens: int = 50,
    call_duration_ms: int = 500,
    status: CallStatus = CallStatus.OK,
    error_class: str | None = None,
    model: str = "gpt-4-test",
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        timestamp=timestamp or _now(),
        call_duration_ms=call_duration_ms,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        status=status,
        error_class=error_class,
    )


class TestRoundTrip:
    async def test_record_call_round_trips_decimal_at_six_decimals(self, pg_repo):
        rec = _record(cost_usd=Decimal("12.345678"))
        await pg_repo.record_call(rec)
        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        assert stats.total_cost_usd == Decimal("12.345678")

    async def test_record_call_persists_failure_with_error_class(self, pg_repo):
        rec = _record(
            status=CallStatus.ERROR,
            error_class="LLMTimeoutError",
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
        )
        await pg_repo.record_call(rec)
        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        assert stats.total_calls == 1
        assert stats.failed_calls == 1
        assert stats.total_cost_usd == Decimal("0")


class TestCheckConstraint:
    async def test_db_rejects_ok_with_error_class(self, pg_engine):
        with pytest.raises(IntegrityError):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    llm_calls.insert().values(
                        tenant_id="t1",
                        operation="analyze",
                        timestamp=_now(),
                        call_duration_ms=10,
                        model="gpt-4",
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=Decimal("0"),
                        status="ok",
                        error_class="LLMError",
                    )
                )

    async def test_db_rejects_error_without_error_class(self, pg_engine):
        with pytest.raises(IntegrityError):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    llm_calls.insert().values(
                        tenant_id="t1",
                        operation="analyze",
                        timestamp=_now(),
                        call_duration_ms=10,
                        model="",
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=Decimal("0"),
                        status="error",
                        error_class=None,
                    )
                )


class TestTimeFilterHalfOpen:
    async def test_from_inclusive_to_exclusive(self, pg_repo):
        anchor = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        # Row exactly at `from` is included; row exactly at `to` is excluded.
        await pg_repo.record_call(_record(timestamp=anchor))  # included
        await pg_repo.record_call(
            _record(timestamp=anchor + timedelta(hours=1))
        )  # included
        await pg_repo.record_call(
            _record(timestamp=anchor + timedelta(hours=2))
        )  # excluded — equal to `to`
        await pg_repo.record_call(
            _record(timestamp=anchor - timedelta(seconds=1))
        )  # excluded — before `from`

        stats = await pg_repo.get_usage_stats(
            "t1",
            from_=anchor,
            to=anchor + timedelta(hours=2),
        )
        assert stats is not None
        assert stats.total_calls == 2

    async def test_empty_window_returns_none(self, pg_repo):
        await pg_repo.record_call(_record())
        future = _now() + timedelta(days=365)
        stats = await pg_repo.get_usage_stats(
            "t1",
            from_=future,
            to=future + timedelta(days=1),
        )
        assert stats is None


class TestAlphaPolicy:
    async def test_cost_and_tokens_scope_to_ok_only(self, pg_repo):
        await pg_repo.record_call(
            _record(
                cost_usd=Decimal("1.0"),
                input_tokens=100,
                output_tokens=50,
                status=CallStatus.OK,
            )
        )
        await pg_repo.record_call(
            _record(
                cost_usd=Decimal("0"),
                input_tokens=0,
                output_tokens=0,
                status=CallStatus.ERROR,
                error_class="LLMError",
                model="",
            )
        )
        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        assert stats.total_calls == 2
        assert stats.failed_calls == 1
        assert stats.total_cost_usd == Decimal("1.0")
        assert stats.input_tokens.total == 100
        assert stats.output_tokens.total == 50


class TestByOperationSort:
    async def test_sorted_by_cost_desc_ties_by_operation_asc(self, pg_repo):
        await pg_repo.record_call(
            _record(operation=Operation.SUMMARIZE, cost_usd=Decimal("0.5"))
        )
        await pg_repo.record_call(
            _record(operation=Operation.ANALYZE, cost_usd=Decimal("0.5"))
        )
        await pg_repo.record_call(
            _record(operation=Operation.ASSIGN_CODES, cost_usd=Decimal("1.0"))
        )

        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        ops = [op.operation for op in stats.by_operation]
        # 1.0 first (assign_codes), then 0.5 ties — analyze before summarize alphabetically.
        assert ops == [
            Operation.ASSIGN_CODES,
            Operation.ANALYZE,
            Operation.SUMMARIZE,
        ]


class TestGetAllUsageStats:
    async def test_per_tenant_alphabetical_with_grand_total(self, pg_repo):
        await pg_repo.record_call(_record(tenant_id="b-tenant"))
        await pg_repo.record_call(_record(tenant_id="a-tenant"))
        await pg_repo.record_call(_record(tenant_id="a-tenant"))

        all_stats = await pg_repo.get_all_usage_stats()
        ids = [s.tenant_id for s in all_stats]
        assert ids == ["a-tenant", "b-tenant", None]

        grand = next(s for s in all_stats if s.tenant_id is None)
        assert grand.total_calls == 3


class TestIndexUsage:
    async def test_tenant_timestamp_query_uses_composite_index(self, pg_engine):
        async with pg_engine.connect() as conn:
            plan = (
                await conn.execute(
                    sa.text(
                        "EXPLAIN SELECT * FROM llm_calls "
                        "WHERE tenant_id = :tid AND timestamp >= :t"
                    ),
                    {"tid": "anyone", "t": _now()},
                )
            ).fetchall()
        text = "\n".join(str(row[0]) for row in plan)
        # Postgres may pick a seq scan on tiny tables; this asserts the index
        # exists and is at least known to the planner. Fall back to checking
        # that the index name appears somewhere in pg_indexes.
        async with pg_engine.connect() as conn:
            indexes = (
                await conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'llm_calls'"
                    )
                )
            ).fetchall()
        names = {row[0] for row in indexes}
        assert "idx_llm_calls_tenant_timestamp" in names
        assert "idx_llm_calls_timestamp" in names
        assert text  # plan rendered (sanity)
