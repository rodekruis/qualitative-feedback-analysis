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
    col: sa.ColumnElement,  # type: ignore[type-arg]
    prefix: str,
) -> list[sa.Label]:  # type: ignore[type-arg]
    """Build labeled aggregation columns for a numeric column.

    Each column is labeled ``{prefix}_{stat}`` so results can be accessed
    by name instead of fragile positional indices.
    """
    return [
        sa.func.avg(col).label(f"{prefix}_avg"),
        sa.func.min(col).label(f"{prefix}_min"),
        sa.func.max(col).label(f"{prefix}_max"),
        sa.func.sum(col).label(f"{prefix}_sum"),
        sa.func.count().label(f"{prefix}_count"),
        sa.func.percentile_cont(0.05).within_group(col).label(f"{prefix}_p5"),
        sa.func.percentile_cont(0.95).within_group(col).label(f"{prefix}_p95"),
    ]


def _parse_distribution(row: sa.Row, prefix: str) -> DistributionStats:  # type: ignore[type-arg]
    """Parse DistributionStats from a named row using the given prefix."""
    m = row._mapping
    return DistributionStats(
        avg=float(m[f"{prefix}_avg"]),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
    )


def _parse_token_stats(row: sa.Row, prefix: str) -> TokenStats:  # type: ignore[type-arg]
    """Parse TokenStats from a named row using the given prefix."""
    m = row._mapping
    return TokenStats(
        avg=float(m[f"{prefix}_avg"]),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        total=int(m[f"{prefix}_sum"]),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
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
            _build_stats_columns(llm_calls.c.call_duration_ms, "dur")
            + _build_stats_columns(llm_calls.c.input_tokens, "inp")
            + _build_stats_columns(llm_calls.c.output_tokens, "out")
        )
        stmt = sa.select(*cols).where(llm_calls.c.tenant_id == tenant_id)

        async with self._session_factory() as session:
            row = (await session.execute(stmt)).one()

        total_calls = int(row._mapping["dur_count"])
        if total_calls == 0:
            return None

        return UsageStats(
            tenant_id=tenant_id,
            total_calls=total_calls,
            call_duration=_parse_distribution(row, "dur"),
            input_tokens=_parse_token_stats(row, "inp"),
            output_tokens=_parse_token_stats(row, "out"),
        )

    async def get_all_usage_stats(self) -> list[UsageStats]:
        """Get per-tenant stats plus a grand total entry.

        Returns
        -------
        list[UsageStats]
            Per-tenant stats followed by a grand total entry (tenant_id=None).
        """
        stats_cols = (
            _build_stats_columns(llm_calls.c.call_duration_ms, "dur")
            + _build_stats_columns(llm_calls.c.input_tokens, "inp")
            + _build_stats_columns(llm_calls.c.output_tokens, "out")
        )
        per_tenant_stmt = sa.select(llm_calls.c.tenant_id, *stats_cols).group_by(
            llm_calls.c.tenant_id
        )
        total_stmt = sa.select(*stats_cols)

        async with self._session_factory() as session:
            tenant_rows = (await session.execute(per_tenant_stmt)).all()
            total_row = (await session.execute(total_stmt)).one()

        results: list[UsageStats] = []
        for row in tenant_rows:
            m = row._mapping
            total_calls = int(m["dur_count"])
            if total_calls == 0:
                continue
            results.append(
                UsageStats(
                    tenant_id=m["tenant_id"],
                    total_calls=total_calls,
                    call_duration=_parse_distribution(row, "dur"),
                    input_tokens=_parse_token_stats(row, "inp"),
                    output_tokens=_parse_token_stats(row, "out"),
                )
            )

        # Grand total
        grand_total_calls = int(total_row._mapping["dur_count"])
        if grand_total_calls > 0:
            results.append(
                UsageStats(
                    tenant_id=None,
                    total_calls=grand_total_calls,
                    call_duration=_parse_distribution(total_row, "dur"),
                    input_tokens=_parse_token_stats(total_row, "inp"),
                    output_tokens=_parse_token_stats(total_row, "out"),
                )
            )

        return results
