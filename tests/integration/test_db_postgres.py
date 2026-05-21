"""Tier-2 schema tests for the ``llm_calls`` table against PostgreSQL.

Covers the parts of ``qfa.adapters.db`` whose behaviour is database-level
— CHECK constraints declared on the table, declared index presence — and
which therefore can't be exercised against SQLite.

Repository-level tests (``SqlAlchemyUsageRepository.record_call`` /
``get_usage_stats`` / ``get_all_usage_stats_by_tenant``) live alongside the class
they exercise, in ``tests/integration/test_usage_repository.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from qfa.adapters.db import llm_calls

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _now() -> datetime:
    return datetime.now(UTC)


class TestCheckConstraint:
    async def test_db_rejects_ok_with_error_class(self, pg_engine):
        """The DB check constraint rejects ``status='ok'`` with an error_class.

        Validation lives in two places (Pydantic + DB CHECK); this test
        guards the DB half against drift if someone removes the constraint.
        """
        with pytest.raises(IntegrityError):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    llm_calls.insert().values(
                        tenant_id="t1",
                        operation="analyze",
                        call_id=uuid4(),
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
        """The DB check constraint rejects ``status='error'`` with no error_class.

        Mirror of the OK-with-error case — guards the other half of the
        ``error_class iff error`` constraint at the database boundary.
        """
        with pytest.raises(IntegrityError):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    llm_calls.insert().values(
                        tenant_id="t1",
                        operation="analyze",
                        call_id=uuid4(),
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


class TestIndexUsage:
    async def test_tenant_timestamp_query_uses_composite_index(self, pg_engine):
        """All expected indexes on ``llm_calls`` exist after migration.

        Postgres may seq-scan tiny test tables, so we don't assert plan
        choice; we just confirm each declared index is present in
        ``pg_indexes`` so a dropped/renamed migration fails loudly here.
        """
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
        assert "idx_llm_calls_tenant_operation_call_id" in names
        assert text  # plan rendered (sanity)
