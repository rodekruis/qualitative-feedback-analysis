"""Tests for the SQLAlchemy usage repository (sqlite-compatible only).

Aggregation tests against real PostgreSQL belong in an integration suite
gated by ``@pytest.mark.integration``. SQLite cannot evaluate
``percentile_cont``, so we only validate inserts and column round-trips
here.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    llm_calls,
    metadata,
)
from qfa.domain.models import CallStatus, LLMCallRecord, Operation

pytestmark = pytest.mark.asyncio


def _make_record(
    tenant_id: str = "tenant-1",
    operation: Operation = Operation.ANALYZE,
    input_tokens: int = 100,
    output_tokens: int = 50,
    call_duration_ms: int = 500,
    model: str = "gpt-4-test",
    cost_usd: Decimal = Decimal("0.0001"),
    status: CallStatus = CallStatus.OK,
    error_class: str | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        timestamp=datetime.now(UTC),
        call_duration_ms=call_duration_ms,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        status=status,
        error_class=error_class,
    )


@pytest.fixture
async def sqlite_repo(tmp_path):
    """Repo backed by an in-memory SQLite database (sync engine wrapper)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    session_factory = create_session_factory(engine)
    repo = SqlAlchemyUsageRepository(session_factory)
    yield repo, engine
    await engine.dispose()


@pytest.fixture
def needs_aiosqlite():
    """Skip if aiosqlite is not installed."""
    pytest.importorskip("aiosqlite")


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_inserts_row(sqlite_repo):
    repo, engine = sqlite_repo
    await repo.record_call(_make_record())
    async with engine.connect() as conn:
        count = (
            await conn.execute(sa.select(sa.func.count()).select_from(llm_calls))
        ).scalar()
    assert count == 1


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_round_trips_all_fields(sqlite_repo):
    repo, engine = sqlite_repo
    rec = _make_record(
        tenant_id="my-tenant",
        operation=Operation.ASSIGN_CODES,
        input_tokens=42,
        output_tokens=7,
        cost_usd=Decimal("1.234567"),
    )
    await repo.record_call(rec)

    async with engine.connect() as conn:
        row = (await conn.execute(sa.select(llm_calls))).one()

    assert row.tenant_id == "my-tenant"
    assert row.operation == "assign_codes"
    assert row.input_tokens == 42
    assert row.output_tokens == 7
    assert row.status == "ok"
    assert row.error_class is None
    assert Decimal(str(row.cost_usd)) == Decimal("1.234567")


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_failure_path_persists_error_class(sqlite_repo):
    repo, engine = sqlite_repo
    rec = _make_record(
        status=CallStatus.ERROR,
        error_class="LLMTimeoutError",
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal("0"),
    )
    await repo.record_call(rec)

    async with engine.connect() as conn:
        row = (await conn.execute(sa.select(llm_calls))).one()

    assert row.status == "error"
    assert row.error_class == "LLMTimeoutError"
