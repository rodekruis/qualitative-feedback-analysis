"""Tests for the SQLAlchemy usage repository (sqlite-compatible only).

Aggregation tests against real PostgreSQL belong in an integration suite
gated by ``@pytest.mark.integration``. SQLite cannot evaluate
``percentile_cont``, so we only validate inserts and column round-trips
here.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr

from qfa.adapters.db import (
    create_session_factory,
    llm_calls,
    metadata,
    resolve_database_url,
)
from qfa.adapters.usage_repository import SqlAlchemyUsageRepository
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
    call_id: UUID | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant_id,
        operation=operation,
        call_id=call_id if call_id is not None else uuid4(),
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
async def sqlite_repo():
    """Repo backed by a true in-memory SQLite database."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
    """Every field of ``LLMCallRecord`` survives the insert/select round-trip.

    Catches regressions in the SQLAlchemy column mapping or repo write —
    e.g., a column rename that compiles but silently drops values.
    """
    repo, engine = sqlite_repo
    fixed_id = uuid4()
    rec = _make_record(
        tenant_id="my-tenant",
        operation=Operation.ASSIGN_CODES,
        input_tokens=42,
        output_tokens=7,
        cost_usd=Decimal("1.234567"),
        call_id=fixed_id,
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
    assert row.call_id == fixed_id


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_persists_distinct_call_ids_across_invocations(sqlite_repo):
    """Distinct call_ids must persist as distinct rows.

    Two records with the same tenant + operation but different call_ids
    persist as two distinct rows, each retaining its own call_id.
    """
    repo, engine = sqlite_repo
    id_a = uuid4()
    id_b = uuid4()
    await repo.record_call(_make_record(call_id=id_a))
    await repo.record_call(_make_record(call_id=id_b))

    async with engine.connect() as conn:
        rows = (
            await conn.execute(sa.select(llm_calls.c.call_id).order_by(llm_calls.c.id))
        ).all()

    persisted_ids = {r.call_id for r in rows}
    assert persisted_ids == {id_a, id_b}


@pytest.mark.usefixtures("needs_aiosqlite")
async def test_record_call_persists_shared_call_id_across_records(sqlite_repo):
    """Shared call_id is preserved across rows (the fan-out case).

    Two records with the same call_id round-trip as two rows sharing that
    call_id — the property #91's aggregation needs.
    """
    repo, engine = sqlite_repo
    shared = uuid4()
    await repo.record_call(_make_record(call_id=shared))
    await repo.record_call(_make_record(call_id=shared))

    async with engine.connect() as conn:
        rows = (await conn.execute(sa.select(llm_calls.c.call_id))).all()

    assert [r.call_id for r in rows] == [shared, shared]


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

    from qfa.adapters.usage_repository import _translate_db_errors
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
        url="postgresql+asyncpg://user:pass@host:5432/qfa",
    )
    assert (
        resolve_database_url(settings) == "postgresql+asyncpg://user:pass@host:5432/qfa"
    )


def test_usage_stats_new_shape_has_llm_call_stats_and_operations():
    """UsageStats carries the new llm_call_stats and operations fields.

    Verifies the domain shape directly (not via DB query) so this test
    runs without any database. The per-invocation aggregation correctness
    and the zero-window path against a real Postgres are covered in
    tests/integration/test_db_postgres.py. (SQLite does not support
    GROUPING SETS so the DB-backed zero-window path cannot be tested here.)
    """
    from decimal import Decimal

    from qfa.domain.models import (
        DistributionStats,
        UsageMetrics,
        UsageStats,
    )

    zero_metrics = UsageMetrics(
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
    )
    stats = UsageStats(
        tenant_id="tenant-1",
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        input_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        llm_call_stats=zero_metrics,
        operations=(),
    )
    assert stats.tenant_id == "tenant-1"
    assert stats.total_calls == 0
    assert stats.llm_call_stats.total_calls == 0
    assert stats.operations == ()


def test_grouping_sets_clause_cross_tenant_emits_full_cube():
    """All-tenants query must emit CUBE over both axes (= 4-set powerset).

    Why: ``/v1/usage/all`` needs grand-total-per-operation rows
    (``tenant_id IS NULL`` with ``operation`` bound) to populate the
    ``operations`` list on the grand-total entry. ``CUBE(tenant_id,
    operation)`` expands in Postgres to the four grouping sets
    ``(tenant_id, operation), (tenant_id), (operation), ()`` — without
    the ``(operation)`` cell, composition has no source for the
    grand-total ``operations`` rows, a regression invisible to
    SQLite-backed unit tests because SQLite doesn't support these
    grouping extensions at all.
    """
    clause = SqlAlchemyUsageRepository._grouping_sets_clause(
        group_by_tenant=True, group_by_operation=True
    )
    assert clause is not None
    assert clause.name == "cube"
    arg_names = [str(arg) for arg in clause.clauses]
    assert arg_names == ["tenant_id", "operation"]


def test_grouping_sets_clause_single_tenant_omits_tenant_axis():
    """Single-tenant query emits CUBE over the operation axis only.

    ``CUBE(operation)`` expands to ``GROUPING SETS ((operation), ())``,
    giving the per-operation rollup plus the grand total for one tenant.
    """
    clause = SqlAlchemyUsageRepository._grouping_sets_clause(
        group_by_tenant=False, group_by_operation=True
    )
    assert clause is not None
    assert clause.name == "cube"
    arg_names = [str(arg) for arg in clause.clauses]
    assert arg_names == ["operation"]


async def test_resolve_database_url_from_password_parts():
    settings = DatabaseSettings(
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
