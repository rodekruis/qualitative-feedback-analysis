"""Tests for the SQLAlchemy usage repository."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    llm_calls,
    metadata,
)
from qfa.domain.models import LLMCallRecord

# SQLite doesn't support percentile_cont, so we only test basic insert
# and query structure here. Full aggregation tests require PostgreSQL.

pytestmark = pytest.mark.asyncio


def _make_record(
    tenant_id: str = "tenant-1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    call_duration_ms: int = 500,
    model: str = "gpt-4-test",
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        timestamp=datetime.now(UTC),
        call_duration_ms=call_duration_ms,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@pytest.fixture
async def sqlite_repo(tmp_path):
    """Create a repo backed by an in-memory SQLite database (sync engine wrapper).

    Note: SQLite doesn't support percentile_cont, so aggregation tests are
    skipped. This fixture validates basic insert/schema operations.
    """
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
    record = _make_record()
    await repo.record_call(record)

    async with engine.connect() as conn:
        result = await conn.execute(sa.select(sa.func.count()).select_from(llm_calls))
        count = result.scalar()

    assert count == 1


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_stores_correct_data(sqlite_repo):
    repo, engine = sqlite_repo
    record = _make_record(tenant_id="my-tenant", input_tokens=42, output_tokens=7)
    await repo.record_call(record)

    async with engine.connect() as conn:
        result = await conn.execute(sa.select(llm_calls))
        row = result.one()

    assert row.tenant_id == "my-tenant"
    assert row.input_tokens == 42
    assert row.output_tokens == 7
