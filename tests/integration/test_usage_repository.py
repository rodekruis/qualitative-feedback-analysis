"""Tier-2 aggregation tests for ``SqlAlchemyUsageRepository`` against PostgreSQL.

These exercise the queries that ``percentile_cont`` / ``GROUPING SETS``
cannot run on SQLite, plus the per-invocation CTE that is the headline
behaviour of issue #91. They depend on the session-scoped ``pg_repo``
fixture in ``tests/integration/conftest.py`` and are gated by
``@pytest.mark.integration``.

Schema-level tests (CHECK constraints, index presence) live next to the
table definition in ``tests/integration/test_db_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

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
    call_id: UUID | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        call_id=call_id if call_id is not None else uuid4(),
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
        """A Decimal cost survives the write/read cycle to six decimals.

        Postgres stores cost_usd as NUMERIC(12,6); this guards against a
        future migration that drops scale and silently rounds.
        """
        rec = _record(cost_usd=Decimal("12.345678"))
        await pg_repo.record_call(rec)
        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        assert stats.total_cost_usd == Decimal("12.345678")

    async def test_record_call_persists_failure_with_error_class(self, pg_repo):
        """An error record with error_class round-trips and counts as failed."""
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

    async def test_call_id_round_trips_through_postgres(self, pg_repo, pg_engine):
        """A UUID ``call_id`` written via the repo reads back unchanged from PG.

        SQLite uses CHAR(32) for ``sa.Uuid`` whereas Postgres uses the
        native UUID type; this guards the Postgres path specifically.
        """
        fixed = uuid4()
        await pg_repo.record_call(_record(call_id=fixed))
        async with pg_engine.connect() as conn:
            row = (await conn.execute(sa.select(llm_calls.c.call_id))).one()
        assert row.call_id == fixed


class TestTimeFilterHalfOpen:
    async def test_from_inclusive_to_exclusive(self, pg_repo):
        """``from_`` is inclusive and ``to`` is exclusive (half-open window)."""
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


class TestAlphaPolicy:
    async def test_cost_sums_all_rows_tokens_scope_to_ok_only(self, pg_repo):
        """``total_cost_usd`` sums every row (incl. errors); token totals stay ok-only.

        Why: failed attempts may incur a real cost (provider billed for
        tokens consumed before the error, partial streaming, etc.). The
        grand total must reflect what was actually spent, so the SQL
        must not filter cost by status. Token totals and distributions
        still gate on ``status='ok'`` so failures cannot skew them.

        The error row is seeded with a non-zero ``cost_usd`` on purpose
        — the current tracking adapter writes 0, but the SQL must remain
        correct if a future adapter ever records real cost on error.
        """
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
                cost_usd=Decimal("0.5"),
                input_tokens=10,
                output_tokens=5,
                status=CallStatus.ERROR,
                error_class="LLMError",
                model="",
            )
        )
        stats = await pg_repo.get_usage_stats("t1")
        assert stats is not None
        assert stats.total_calls == 2
        assert stats.failed_calls == 1
        assert stats.total_cost_usd == Decimal("1.5")
        assert stats.input_tokens.total == 100
        assert stats.output_tokens.total == 50


class TestGetAllUsageStats:
    async def test_per_tenant_plus_grand_total_with_operations(self, pg_repo):
        """get_all_usage_stats_by_tenant returns per-tenant + grand-total entries with operations.

        Two tenants, overlapping operations. Verifies:
        - tenants returned alphabetically by tenant_id
        - grand-total entry appended last (tenant_id is None)
        - grand-total has operations rolled up across tenants
        - per-tenant operations are isolated to that tenant
        """
        await pg_repo.record_call(
            _record(tenant_id="a-tenant", operation=Operation.ANALYZE)
        )
        await pg_repo.record_call(
            _record(tenant_id="a-tenant", operation=Operation.SUMMARIZE)
        )
        await pg_repo.record_call(
            _record(tenant_id="b-tenant", operation=Operation.ANALYZE)
        )

        all_stats = await pg_repo.get_all_usage_stats_by_tenant()
        ids = [s.tenant_id for s in all_stats]
        assert ids == ["a-tenant", "b-tenant", None]

        a = next(s for s in all_stats if s.tenant_id == "a-tenant")
        assert {op.operation for op in a.operations} == {
            Operation.ANALYZE,
            Operation.SUMMARIZE,
        }

        b = next(s for s in all_stats if s.tenant_id == "b-tenant")
        assert {op.operation for op in b.operations} == {Operation.ANALYZE}

        grand = next(s for s in all_stats if s.tenant_id is None)
        assert grand.total_calls == 3
        # Grand total carries operations rolled up across tenants.
        assert {op.operation for op in grand.operations} == {
            Operation.ANALYZE,
            Operation.SUMMARIZE,
        }
        # analyze appears in both tenants ⇒ grand total operation total_calls == 2
        analyze = next(
            op for op in grand.operations if op.operation == Operation.ANALYZE
        )
        assert analyze.total_calls == 2


class TestGetAllUsageByOperation:
    async def test_per_operation_plus_grand_total_with_tenants(self, pg_repo):
        """get_all_usage_by_operation returns per-operation + grand-total with tenants.

        Inverse hierarchy of get_all_usage_stats_by_tenant: top-level keyed by
        operation, with per-tenant breakdown nested under each. Two
        operations, overlapping tenants. Verifies:
        - operations returned sorted by cost desc (ties broken by
          operation asc)
        - grand-total entry appended last (operation is None)
        - grand-total has total_calls summing across operations
        - per-operation tenants are isolated to that operation and
          sorted by cost desc
        """
        await pg_repo.record_call(
            _record(
                tenant_id="a-tenant",
                operation=Operation.ANALYZE,
                cost_usd=Decimal("0.3"),
            )
        )
        await pg_repo.record_call(
            _record(
                tenant_id="b-tenant",
                operation=Operation.ANALYZE,
                cost_usd=Decimal("0.2"),
            )
        )
        await pg_repo.record_call(
            _record(
                tenant_id="a-tenant",
                operation=Operation.SUMMARIZE,
                cost_usd=Decimal("0.1"),
            )
        )

        all_stats = await pg_repo.get_all_usage_by_operation()
        ops = [s.operation for s in all_stats]
        # analyze (0.5) > summarize (0.1); grand total last.
        assert ops == [Operation.ANALYZE, Operation.SUMMARIZE, None]

        analyze = next(s for s in all_stats if s.operation == Operation.ANALYZE)
        assert [t.tenant_id for t in analyze.tenants] == ["a-tenant", "b-tenant"]
        assert analyze.total_calls == 2

        summarize = next(s for s in all_stats if s.operation == Operation.SUMMARIZE)
        assert [t.tenant_id for t in summarize.tenants] == ["a-tenant"]

        grand = next(s for s in all_stats if s.operation is None)
        assert grand.total_calls == 3
        # Symmetric to the by-tenant grand total carrying ``operations``
        # across tenants: the by-operation grand total carries
        # ``tenants`` rolled up across operations. Two tenants in total,
        # sorted by cost desc (a-tenant: 0.3+0.1=0.4 > b-tenant: 0.2).
        assert [t.tenant_id for t in grand.tenants] == ["a-tenant", "b-tenant"]
        a = next(t for t in grand.tenants if t.tenant_id == "a-tenant")
        assert a.total_calls == 2

    async def test_empty_window_returns_zero_total(self, pg_repo):
        """Empty window ⇒ no per-operation rows and a zero grand-total entry.

        Mirrors get_all_usage_stats_by_tenant' empty-window contract: the
        grand-total entry is always present, never silently dropped.
        """
        all_stats = await pg_repo.get_all_usage_by_operation()
        non_total = [s for s in all_stats if s.operation is not None]
        assert non_total == []
        grand = next(s for s in all_stats if s.operation is None)
        assert grand.total_calls == 0
        assert grand.tenants == ()


class TestPerInvocationAggregation:
    async def test_inherited_metrics_group_by_call_id(self, pg_repo):
        """One call_id with 3 LLM rows aggregates to one invocation.

        Inherited ``call_duration`` sums to 900ms (single point ⇒ avg=min=max=900);
        ``llm_call_stats`` keeps the three individual rows {200, 300, 400}.
        This is the headline behaviour change vs. the previous schema —
        directly validates that ``call_id`` is the per-invocation grouping key.
        """
        shared = uuid4()
        await pg_repo.record_call(_record(call_id=shared, call_duration_ms=200))
        await pg_repo.record_call(_record(call_id=shared, call_duration_ms=300))
        await pg_repo.record_call(_record(call_id=shared, call_duration_ms=400))

        stats = await pg_repo.get_usage_stats("t1")

        # Per-invocation: one invocation with summed duration.
        assert stats.total_calls == 1
        assert stats.call_duration.avg == pytest.approx(900.0)
        assert stats.call_duration.min == 900
        assert stats.call_duration.max == 900

        # Per-LLM-call: three rows, with their own distribution.
        assert stats.llm_call_stats.total_calls == 3
        assert stats.llm_call_stats.call_duration.min == 200
        assert stats.llm_call_stats.call_duration.max == 400

    async def test_failed_calls_per_invocation_semantics(self, pg_repo):
        """failed_calls (per-invocation) counts only all-failed call_ids.

        call_id A: 1 ok + 1 error ⇒ NOT counted in per-invocation failed_calls
        (mixed); per-LLM-call failed_calls still counts the one error row.
        call_id B: 2 errors ⇒ counted as ONE failed invocation.
        """
        a = uuid4()
        b = uuid4()
        await pg_repo.record_call(_record(call_id=a, status=CallStatus.OK))
        await pg_repo.record_call(
            _record(
                call_id=a,
                status=CallStatus.ERROR,
                error_class="LLMError",
                cost_usd=Decimal("0"),
                input_tokens=0,
                output_tokens=0,
                model="",
            )
        )
        for _ in range(2):
            await pg_repo.record_call(
                _record(
                    call_id=b,
                    status=CallStatus.ERROR,
                    error_class="LLMError",
                    cost_usd=Decimal("0"),
                    input_tokens=0,
                    output_tokens=0,
                    model="",
                )
            )

        stats = await pg_repo.get_usage_stats("t1")

        assert stats.total_calls == 2  # A and B
        assert stats.failed_calls == 1  # only B is all-failed
        assert stats.llm_call_stats.total_calls == 4
        assert stats.llm_call_stats.failed_calls == 3

    async def test_all_failed_excluded_from_distribution_but_summed_in_cost(
        self, pg_repo
    ):
        """All-failed invocation: out of distributions, in ``total_cost_usd``.

        Mirrors the per-LLM-call convention "distributions exclude failures,
        counts include them" — and extends it: ``total_cost_usd`` sums every
        invocation including all-failed ones, so the grand total reflects
        actual spend even when a provider billed for a request that errored.
        The all-failed row is seeded with a non-zero ``cost_usd`` to
        exercise that path (the current tracking adapter writes 0, but the
        SQL must remain correct if a future adapter records real cost).
        """
        ok = uuid4()
        bad = uuid4()
        await pg_repo.record_call(
            _record(
                call_id=ok,
                status=CallStatus.OK,
                call_duration_ms=500,
                cost_usd=Decimal("1.0"),
            )
        )
        await pg_repo.record_call(
            _record(
                call_id=bad,
                status=CallStatus.ERROR,
                error_class="LLMError",
                call_duration_ms=9999,
                cost_usd=Decimal("0.25"),
                input_tokens=0,
                output_tokens=0,
                model="",
            )
        )

        stats = await pg_repo.get_usage_stats("t1")

        assert stats.total_calls == 2
        assert stats.failed_calls == 1
        # The 9999ms all-failed row must not appear in the distribution.
        assert stats.call_duration.max == 500
        # But the per-LLM-call view still counts it.
        assert stats.llm_call_stats.total_calls == 2
        assert stats.llm_call_stats.failed_calls == 1
        # Cost sums OK + all-failed (1.0 + 0.25) at both views.
        assert stats.total_cost_usd == Decimal("1.25")
        assert stats.llm_call_stats.total_cost_usd == Decimal("1.25")


class TestOperationsBreakdown:
    async def test_operations_sorted_by_cost_desc_then_name(self, pg_repo):
        """``operations`` is sorted by total_cost_usd desc; ties by operation asc.

        Seeded so summarize > analyze on cost, and analyze comes before
        summarize alphabetically — the cost-desc primary key wins. A
        third (assign_codes) ties summarize on cost to exercise the
        alphabetical tie-break.
        """
        await pg_repo.record_call(
            _record(operation=Operation.ANALYZE, cost_usd=Decimal("0.1"))
        )
        await pg_repo.record_call(
            _record(operation=Operation.SUMMARIZE, cost_usd=Decimal("0.5"))
        )
        await pg_repo.record_call(
            _record(operation=Operation.ASSIGN_CODES, cost_usd=Decimal("0.5"))
        )

        stats = await pg_repo.get_usage_stats("t1")

        # Expected order: assign_codes (0.5), summarize (0.5), analyze (0.1)
        ops = [op.operation for op in stats.operations]
        assert ops == [
            Operation.ASSIGN_CODES,
            Operation.SUMMARIZE,
            Operation.ANALYZE,
        ]

    async def test_empty_operations_omitted(self, pg_repo):
        """Operations with zero calls in the window are omitted from ``operations``.

        Tenant called only ``analyze`` ⇒ ``operations`` has length 1 and
        ``summarize`` / ``assign_codes`` / ``summarize_aggregate`` are absent.
        Mirrors the precedent in db.py:391 that filters zero-call tenants.
        """
        await pg_repo.record_call(_record(operation=Operation.ANALYZE))

        stats = await pg_repo.get_usage_stats("t1")

        assert len(stats.operations) == 1
        assert stats.operations[0].operation == Operation.ANALYZE

    async def test_per_operation_llm_call_stats_populated(self, pg_repo):
        """Each OperationStats carries its own ``llm_call_stats`` block.

        Multi-LLM-call invocation under one operation: one invocation but
        three LLM rows. Per-operation ``total_calls == 1``, per-operation
        ``llm_call_stats.total_calls == 3``. Lets clients compute fan-out
        per operation.
        """
        shared = uuid4()
        for _ in range(3):
            await pg_repo.record_call(
                _record(operation=Operation.ASSIGN_CODES, call_id=shared)
            )

        stats = await pg_repo.get_usage_stats("t1")

        assert len(stats.operations) == 1
        op = stats.operations[0]
        assert op.operation == Operation.ASSIGN_CODES
        assert op.total_calls == 1
        assert op.llm_call_stats.total_calls == 3
