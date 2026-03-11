"""SQLAlchemy-based usage repository for LLM call tracking."""

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qfa.domain.models import (
    DistributionStats,
    LLMCallRecord,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import UsageRepositoryPort

metadata = sa.MetaData()

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.String, nullable=False, index=True),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("call_duration_ms", sa.Integer, nullable=False),
    sa.Column("model", sa.String, nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
)


def create_async_engine_from_url(url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine from a database URL.

    Parameters
    ----------
    url : str
        The database connection URL.

    Returns
    -------
    AsyncEngine
        The configured async engine.
    """
    return create_async_engine(url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to the given engine.

    Parameters
    ----------
    engine : AsyncEngine
        The async engine to bind sessions to.

    Returns
    -------
    async_sessionmaker[AsyncSession]
        A factory for creating async sessions.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def _build_stats_columns(
    col: sa.Column,  # type: ignore[type-arg]
) -> list[sa.Function]:  # type: ignore[type-arg]
    """Build aggregation columns for a numeric column.

    Returns [avg, min, max, sum, count, p5, p95].
    """
    return [
        sa.func.avg(col),
        sa.func.min(col),
        sa.func.max(col),
        sa.func.sum(col),
        sa.func.count(),
        sa.func.percentile_cont(0.05).within_group(col),
        sa.func.percentile_cont(0.95).within_group(col),
    ]


def _parse_distribution(row: sa.Row, offset: int) -> DistributionStats:  # type: ignore[type-arg]
    """Parse DistributionStats from a row starting at offset.

    Expects columns: avg, min, max, sum, count, p5, p95.
    Returns DistributionStats using avg, min, max, p5, p95.
    """
    return DistributionStats(
        avg=float(row[offset]),
        min=float(row[offset + 1]),
        max=float(row[offset + 2]),
        p5=float(row[offset + 5]),
        p95=float(row[offset + 6]),
    )


def _parse_token_stats(row: sa.Row, offset: int) -> TokenStats:  # type: ignore[type-arg]
    """Parse TokenStats from a row starting at offset.

    Expects columns: avg, min, max, sum, count, p5, p95.
    """
    return TokenStats(
        avg=float(row[offset]),
        min=float(row[offset + 1]),
        max=float(row[offset + 2]),
        total=int(row[offset + 3]),
        p5=float(row[offset + 5]),
        p95=float(row[offset + 6]),
    )


class SqlAlchemyUsageRepository(UsageRepositoryPort):
    """Usage repository backed by SQLAlchemy and PostgreSQL.

    Parameters
    ----------
    session_factory : Callable[..., AsyncSession]
        Factory for creating async database sessions.
    """

    def __init__(self, session_factory: Callable[..., AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_call(self, record: LLMCallRecord) -> None:
        """Insert a single LLM call record.

        Parameters
        ----------
        record : LLMCallRecord
            The call record to persist.
        """
        async with self._session_factory() as session:
            await session.execute(
                llm_calls.insert().values(
                    tenant_id=record.tenant_id,
                    timestamp=record.timestamp,
                    call_duration_ms=record.call_duration_ms,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                )
            )
            await session.commit()

    async def get_usage_stats(self, tenant_id: str) -> UsageStats | None:
        """Get aggregated usage stats for a single tenant.

        Parameters
        ----------
        tenant_id : str
            The tenant to query.

        Returns
        -------
        UsageStats | None
            Stats for the tenant, or None if no calls recorded.
        """
        cols = (
            _build_stats_columns(llm_calls.c.call_duration_ms)
            + _build_stats_columns(llm_calls.c.input_tokens)
            + _build_stats_columns(llm_calls.c.output_tokens)
        )
        stmt = sa.select(*cols).where(llm_calls.c.tenant_id == tenant_id)

        async with self._session_factory() as session:
            row = (await session.execute(stmt)).one()

        # count is at index 4
        total_calls = int(row[4])
        if total_calls == 0:
            return None

        return UsageStats(
            tenant_id=tenant_id,
            total_calls=total_calls,
            call_duration=_parse_distribution(row, 0),
            input_tokens=_parse_token_stats(row, 7),
            output_tokens=_parse_token_stats(row, 14),
        )

    async def get_all_usage_stats(self) -> list[UsageStats]:
        """Get per-tenant stats plus a grand total entry.

        Returns
        -------
        list[UsageStats]
            Per-tenant stats followed by a grand total entry (tenant_id=None).
        """
        cols = [llm_calls.c.tenant_id] + (
            _build_stats_columns(llm_calls.c.call_duration_ms)
            + _build_stats_columns(llm_calls.c.input_tokens)
            + _build_stats_columns(llm_calls.c.output_tokens)
        )
        per_tenant_stmt = sa.select(*cols).group_by(llm_calls.c.tenant_id)

        total_cols = (
            _build_stats_columns(llm_calls.c.call_duration_ms)
            + _build_stats_columns(llm_calls.c.input_tokens)
            + _build_stats_columns(llm_calls.c.output_tokens)
        )
        total_stmt = sa.select(*total_cols)

        async with self._session_factory() as session:
            tenant_rows = (await session.execute(per_tenant_stmt)).all()
            total_row = (await session.execute(total_stmt)).one()

        results: list[UsageStats] = []
        for row in tenant_rows:
            total_calls = int(row[5])  # count is at offset 5 (tenant_id + 4 cols)
            if total_calls == 0:
                continue
            results.append(
                UsageStats(
                    tenant_id=row[0],
                    total_calls=total_calls,
                    call_duration=_parse_distribution(row, 1),
                    input_tokens=_parse_token_stats(row, 8),
                    output_tokens=_parse_token_stats(row, 15),
                )
            )

        # Grand total
        grand_total_calls = int(total_row[4])
        if grand_total_calls > 0:
            results.append(
                UsageStats(
                    tenant_id=None,
                    total_calls=grand_total_calls,
                    call_duration=_parse_distribution(total_row, 0),
                    input_tokens=_parse_token_stats(total_row, 7),
                    output_tokens=_parse_token_stats(total_row, 14),
                )
            )

        return results
