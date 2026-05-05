"""SQLAlchemy-based usage repository for LLM call tracking."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import sqlalchemy as sa
from azure.identity import DefaultAzureCredential
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qfa.domain.errors import UsageRepositoryUnavailableError
from qfa.domain.models import (
    DistributionStats,
    LLMCallRecord,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import UsageRepositoryPort
from qfa.settings import DatabaseSettings


@asynccontextmanager
async def _translate_db_errors() -> AsyncIterator[None]:
    """Translate SQLAlchemy connectivity errors into a domain-level error.

    Wraps the read paths so the API layer can map a single domain
    exception (``UsageRepositoryUnavailableError``) to ``503 {"code":
    "usage_backend_unavailable"}`` without importing SQLAlchemy. Write
    paths (``record_call``) are intentionally not wrapped: the
    ``TrackingLLMAdapter`` already swallows recording failures so the
    LLM response still flows back to the user.
    """
    try:
        yield
    except (OperationalError, InterfaceError) as exc:
        raise UsageRepositoryUnavailableError(str(exc)) from exc


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


class _AadTokenProvider:
    """Cache AAD access tokens and refresh before expiry."""

    def __init__(self, scope: str) -> None:
        self._scope = scope
        self._credential = DefaultAzureCredential()
        self._token: str | None = None
        self._expires_on: float = 0

    def get_token(self) -> str:
        now = datetime.now(UTC).timestamp()
        if self._token is not None and now < (self._expires_on - 120):
            return self._token

        token = self._credential.get_token(self._scope)
        self._token = token.token
        self._expires_on = float(token.expires_on)
        return token.token


def resolve_database_url(settings: DatabaseSettings) -> str:
    """Resolve an SQLAlchemy database URL from DB settings.

    If ``settings.url`` is provided, it is returned unchanged.
    Otherwise the URL is assembled from host/user/port/name and auth mode.
    """
    if settings.url:
        return settings.url

    user = quote(settings.user)
    host = settings.host
    port = settings.port
    name = settings.name

    if settings.auth_mode == "entra":
        return f"postgresql+asyncpg://{user}@{host}:{port}/{name}?ssl=require"

    password = ""
    if settings.password is not None:
        password = quote(settings.password.get_secret_value())
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def create_async_engine_from_settings(settings: DatabaseSettings) -> AsyncEngine:
    """Create an async engine from app DB settings.

    In ``entra`` mode, a fresh AAD access token is injected on each new
    physical connection via SQLAlchemy's ``do_connect`` hook.
    """
    url = resolve_database_url(settings)
    engine = create_async_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

    if settings.auth_mode != "entra":
        return engine

    token_provider = _AadTokenProvider(settings.aad_scope)

    @sa.event.listens_for(engine.sync_engine, "do_connect")
    def _inject_aad_token(_dialect, _conn_rec, _cargs, cparams) -> None:  # noqa: ANN001
        cparams["password"] = token_provider.get_token()

    return engine


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
    *,
    where: sa.ColumnElement | None = None,
) -> list[sa.Label]:
    """Build labeled aggregation columns for a numeric column.

    Each column is labeled ``{prefix}_{stat}`` so results can be accessed
    by name instead of fragile positional indices. When ``where`` is
    supplied, ``FILTER (WHERE ...)`` is applied to every aggregate so the
    same SELECT can mix all-row counts with ok-only distributions.
    """

    def _f(agg: sa.ColumnElement) -> sa.ColumnElement:
        return agg.filter(where) if where is not None else agg

    return [
        _f(sa.func.avg(col)).label(f"{prefix}_avg"),
        _f(sa.func.min(col)).label(f"{prefix}_min"),
        _f(sa.func.max(col)).label(f"{prefix}_max"),
        _f(sa.func.sum(col)).label(f"{prefix}_sum"),
        _f(sa.func.count()).label(f"{prefix}_count"),
        _f(sa.func.percentile_cont(0.05).within_group(col)).label(f"{prefix}_p5"),
        _f(sa.func.percentile_cont(0.95).within_group(col)).label(f"{prefix}_p95"),
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


def _row_to_usage_stats(tenant_id: str | None, row: sa.Row) -> UsageStats:
    """Assemble a ``UsageStats`` from a totals row."""
    m = row._mapping
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=int(m["total_calls"]),
        failed_calls=int(m["failed_calls"] or 0),
        total_cost_usd=Decimal(str(m["total_cost_usd"])),
        call_duration=_parse_distribution_ok(row, "dur"),
        input_tokens=_parse_token_stats_ok(row, "inp"),
        output_tokens=_parse_token_stats_ok(row, "out"),
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

        async with _translate_db_errors(), self._session_factory() as session:
            t_row = (
                await session.execute(
                    sa.select(
                        sa.func.count().label("total_calls"),
                        sa.func.count()
                        .filter(llm_calls.c.status == "error")
                        .label("failed_calls"),
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

        return UsageStats(
            tenant_id=tenant_id,
            total_calls=total_calls,
            failed_calls=failed_calls,
            total_cost_usd=Decimal(str(ok_row._mapping["total_cost_usd"])),
            call_duration=_parse_distribution_ok(ok_row, "dur"),
            input_tokens=_parse_token_stats_ok(ok_row, "inp"),
            output_tokens=_parse_token_stats_ok(ok_row, "out"),
        )

    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Per-tenant stats plus a grand total entry (tenant_id=None).

        Tenants are returned alphabetically by tenant_id; the grand-total
        entry is appended last. Implemented as a single ``GROUPING SETS``
        query (Postgres-only) so cost is O(1) round-trips regardless of
        tenant count.
        """
        base_pred: list = []
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        ok_filter = llm_calls.c.status == "ok"
        err_filter = llm_calls.c.status == "error"

        # Per-tenant totals + distributions, plus grand-total roll-up via
        # GROUPING SETS ((tenant_id), ()). The roll-up row carries
        # tenant_id IS NULL, which matches UsageStats(tenant_id=None, ...).
        totals_q = (
            sa.select(
                llm_calls.c.tenant_id,
                sa.func.count().label("total_calls"),
                sa.func.count().filter(err_filter).label("failed_calls"),
                sa.func.coalesce(
                    sa.func.sum(llm_calls.c.cost_usd).filter(ok_filter), 0
                ).label("total_cost_usd"),
                *_build_stats_columns(
                    llm_calls.c.call_duration_ms, "dur", where=ok_filter
                ),
                *_build_stats_columns(llm_calls.c.input_tokens, "inp", where=ok_filter),
                *_build_stats_columns(
                    llm_calls.c.output_tokens, "out", where=ok_filter
                ),
            )
            .where(*base_pred)
            .group_by(sa.text("GROUPING SETS ((tenant_id), ())"))
            .order_by(llm_calls.c.tenant_id.asc().nulls_last())
        )

        async with _translate_db_errors(), self._session_factory() as session:
            totals_rows = (await session.execute(totals_q)).all()

        results: list[UsageStats] = []
        for r in totals_rows:
            # Skip the grand-total row when the window is empty: GROUPING SETS
            # always emits the () row even with zero source rows, but the prior
            # contract was to return [] in that case.
            if int(r._mapping["total_calls"]) == 0:
                continue
            results.append(_row_to_usage_stats(r._mapping["tenant_id"], r))
        return results
