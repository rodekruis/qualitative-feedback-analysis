"""Tests for the SQLAlchemy usage repository (sqlite-compatible only).

Aggregation tests against real PostgreSQL belong in an integration suite
gated by ``@pytest.mark.integration``. SQLite cannot evaluate
``percentile_cont``, so we only validate inserts and column round-trips
here.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    llm_calls,
    metadata,
    resolve_database_url,
)
from qfa.domain.models import CallStatus, LLMCallRecord, Operation
from qfa.settings import DatabaseSettings

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


@pytest_asyncio.fixture
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


async def test_translate_db_errors_maps_sqlalchemy_exceptions_to_domain():
    """SQLAlchemy connectivity errors must surface as the domain error.

    The API layer maps the domain error to ``503 usage_backend_unavailable``;
    if SQLAlchemy exceptions ever leak past this boundary the response
    becomes a generic 500 and consumers lose the ability to retry.
    """
    from sqlalchemy.exc import InterfaceError, OperationalError

    from qfa.adapters.db import _translate_db_errors
    from qfa.domain.errors import UsageRepositoryUnavailableError

    with pytest.raises(UsageRepositoryUnavailableError):
        async with _translate_db_errors():
            raise OperationalError("conn", {}, Exception("connection refused"))

    with pytest.raises(UsageRepositoryUnavailableError):
        async with _translate_db_errors():
            raise InterfaceError("conn", {}, Exception("interface gone"))

    # Non-connectivity exceptions must pass through untouched.
    with pytest.raises(ValueError):
        async with _translate_db_errors():
            raise ValueError("not a connectivity issue")


async def test_resolve_database_url_uses_explicit_url():
    settings = DatabaseSettings(
        track_usage=True,
        url="postgresql+asyncpg://user:pass@host:5432/qfa",
    )
    assert (
        resolve_database_url(settings) == "postgresql+asyncpg://user:pass@host:5432/qfa"
    )


async def test_resolve_database_url_from_password_parts():
    settings = DatabaseSettings(
        track_usage=True,
        host="db.internal",
        port=5432,
        name="qfa",
        user="qfaadmin",
        password=SecretStr("secret"),
    )
    assert (
        resolve_database_url(settings)
        == "postgresql+asyncpg://qfaadmin:secret@db.internal:5432/qfa"
    )


async def test_resolve_database_url_from_entra_parts():
    settings = DatabaseSettings(
        track_usage=True,
        auth_mode="entra",
        host="db.internal",
        port=5432,
        name="qfa",
        user="app-msi",
    )
    assert (
        resolve_database_url(settings)
        == "postgresql+asyncpg://app-msi@db.internal:5432/qfa?ssl=require"
    )
