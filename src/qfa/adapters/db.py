"""SQLAlchemy-based usage repository for LLM call tracking."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

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
    Operation,
    OperationStats,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import UsageRepositoryPort

metadata = sa.MetaData()

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("tenant_id", sa.String(255), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("call_duration_ms", sa.Integer, nullable=False),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False, default=0),
    sa.Column("output_tokens", sa.Integer, nullable=False, default=0),
    sa.Column(
        "cost_usd",
        sa.Numeric(precision=12, scale=6),
        nullable=False,
        default=Decimal("0"),
    ),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error_class", sa.String(128), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Index("idx_llm_calls_tenant_timestamp", "tenant_id", "timestamp"),
    sa.Index("idx_llm_calls_timestamp", "timestamp"),
)


def create_async_engine_from_url(url: str) -> AsyncEngine:
    """Create a tuned async engine with pre-ping + recycle for managed PG.

    Parameters
    ----------
    url : str
        The database connection URL.

    Returns
    -------
    AsyncEngine
        The configured async engine.
    """
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


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
    col: sa.ColumnElement,
    prefix: str,
) -> list[sa.Label]:
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


def _parse_distribution_ok(row: sa.Row, prefix: str) -> DistributionStats:
    """Parse DistributionStats from a row whose aggregates are over ok-only rows.

    When no ok rows exist, ``avg`` is NULL — return zeros.
    """
    m = row._mapping
    avg = m[f"{prefix}_avg"]
    if avg is None:
        return DistributionStats(avg=0, min=0, max=0, p5=0, p95=0)
    return DistributionStats(
        avg=float(avg),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
    )


def _parse_token_stats_ok(row: sa.Row, prefix: str) -> TokenStats:
    """Parse TokenStats from a row whose aggregates are over ok-only rows."""
    m = row._mapping
    avg = m[f"{prefix}_avg"]
    if avg is None:
        return TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0)
    return TokenStats(
        avg=float(avg),
        min=float(m[f"{prefix}_min"]),
        max=float(m[f"{prefix}_max"]),
        total=int(m[f"{prefix}_sum"] or 0),
        p5=float(m[f"{prefix}_p5"]),
        p95=float(m[f"{prefix}_p95"]),
    )


def _build_by_operation(rows) -> tuple[OperationStats, ...]:  # noqa: ANN001
    """Build sorted by-operation entries.

    Sort: ``cost_usd`` desc, ties broken by ``operation`` asc. Unknown
    operation strings (rows that predate per-op tracking) coerce to
    ``Operation.UNKNOWN``.
    """
    items: list[OperationStats] = []
    for r in rows:
        m = r._mapping
        op_raw = str(m["operation"])
        try:
            op_enum = Operation(op_raw)
        except ValueError:
            op_enum = Operation.UNKNOWN
        items.append(
            OperationStats(
                operation=op_enum,
                total_calls=int(m["total_calls"]),
                failed_calls=int(m["failed_calls"] or 0),
                cost_usd=Decimal(str(m["cost_usd"])),
                input_tokens_total=int(m["input_tokens_total"] or 0),
                output_tokens_total=int(m["output_tokens_total"] or 0),
            )
        )
    items.sort(key=lambda s: (-s.cost_usd, str(s.operation)))
    return tuple(items)


def _by_operation_select(base_pred: list) -> sa.Select:
    """Build the per-operation aggregation SELECT for the given base predicates."""
    return (
        sa.select(
            llm_calls.c.operation,
            sa.func.count().label("total_calls"),
            sa.func.sum(sa.case((llm_calls.c.status == "error", 1), else_=0)).label(
                "failed_calls"
            ),
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (llm_calls.c.status == "ok", llm_calls.c.cost_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("cost_usd"),
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (llm_calls.c.status == "ok", llm_calls.c.input_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("input_tokens_total"),
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (llm_calls.c.status == "ok", llm_calls.c.output_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("output_tokens_total"),
        )
        .where(*base_pred)
        .group_by(llm_calls.c.operation)
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
        """Insert a single LLM call attempt record."""
        async with self._session_factory() as session:
            await session.execute(
                llm_calls.insert().values(
                    tenant_id=record.tenant_id,
                    operation=str(record.operation),
                    timestamp=record.timestamp,
                    call_duration_ms=record.call_duration_ms,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_usd=record.cost_usd,
                    status=str(record.status),
                    error_class=record.error_class,
                )
            )
            await session.commit()

    async def get_usage_stats(
        self,
        tenant_id: str,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> UsageStats | None:
        """Aggregate stats for a single tenant within an optional time window.

        Cost, distributions, and token totals scope to ``status='ok'`` rows.
        ``total_calls`` and ``failed_calls`` count all rows in the window.
        """
        base_pred: list = [llm_calls.c.tenant_id == tenant_id]
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        ok_pred = [*base_pred, llm_calls.c.status == "ok"]

        async with self._session_factory() as session:
            t_row = (
                await session.execute(
                    sa.select(
                        sa.func.count().label("total_calls"),
                        sa.func.sum(
                            sa.case((llm_calls.c.status == "error", 1), else_=0)
                        ).label("failed_calls"),
                    ).where(*base_pred)
                )
            ).one()
            total_calls = int(t_row._mapping["total_calls"])
            if total_calls == 0:
                return None
            failed_calls = int(t_row._mapping["failed_calls"] or 0)

            ok_row = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0).label(
                            "total_cost_usd"
                        ),
                        *_build_stats_columns(llm_calls.c.call_duration_ms, "dur"),
                        *_build_stats_columns(llm_calls.c.input_tokens, "inp"),
                        *_build_stats_columns(llm_calls.c.output_tokens, "out"),
                    ).where(*ok_pred)
                )
            ).one()

            per_op = (await session.execute(_by_operation_select(base_pred))).all()

        return UsageStats(
            tenant_id=tenant_id,
            total_calls=total_calls,
            failed_calls=failed_calls,
            total_cost_usd=Decimal(str(ok_row._mapping["total_cost_usd"])),
            call_duration=_parse_distribution_ok(ok_row, "dur"),
            input_tokens=_parse_token_stats_ok(ok_row, "inp"),
            output_tokens=_parse_token_stats_ok(ok_row, "out"),
            by_operation=_build_by_operation(per_op),
        )

    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Per-tenant stats plus a grand total entry (tenant_id=None).

        Tenants are returned alphabetically by tenant_id; the grand-total
        entry is appended last.
        """
        base_pred: list = []
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        async with self._session_factory() as session:
            tenants = (
                await session.execute(
                    sa.select(llm_calls.c.tenant_id)
                    .where(*base_pred)
                    .group_by(llm_calls.c.tenant_id)
                    .order_by(llm_calls.c.tenant_id.asc())
                )
            ).all()

        results: list[UsageStats] = []
        for trow in tenants:
            tid = trow._mapping["tenant_id"]
            stats = await self.get_usage_stats(tid, from_=from_, to=to)
            if stats is not None:
                results.append(stats)

        async with self._session_factory() as session:
            t_row = (
                await session.execute(
                    sa.select(
                        sa.func.count().label("total_calls"),
                        sa.func.sum(
                            sa.case((llm_calls.c.status == "error", 1), else_=0)
                        ).label("failed_calls"),
                    ).where(*base_pred)
                )
            ).one()
            total_calls = int(t_row._mapping["total_calls"])
            if total_calls == 0:
                return results
            failed_calls = int(t_row._mapping["failed_calls"] or 0)

            ok_pred = [*base_pred, llm_calls.c.status == "ok"]
            ok_row = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0).label(
                            "total_cost_usd"
                        ),
                        *_build_stats_columns(llm_calls.c.call_duration_ms, "dur"),
                        *_build_stats_columns(llm_calls.c.input_tokens, "inp"),
                        *_build_stats_columns(llm_calls.c.output_tokens, "out"),
                    ).where(*ok_pred)
                )
            ).one()

            per_op = (await session.execute(_by_operation_select(base_pred))).all()

        results.append(
            UsageStats(
                tenant_id=None,
                total_calls=total_calls,
                failed_calls=failed_calls,
                total_cost_usd=Decimal(str(ok_row._mapping["total_cost_usd"])),
                call_duration=_parse_distribution_ok(ok_row, "dur"),
                input_tokens=_parse_token_stats_ok(ok_row, "inp"),
                output_tokens=_parse_token_stats_ok(ok_row, "out"),
                by_operation=_build_by_operation(per_op),
            )
        )
        return results
