"""SQLAlchemy-based usage repository for LLM call tracking."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import sqlalchemy as sa
import sqlalchemy.event  # ensure sa.event is available to type checkers
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
    Operation,
    OperationStats,
    TokenStats,
    UsageMetrics,
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
    sa.Column(
        "call_id",
        sa.Uuid(),
        nullable=False,
        comment=(
            "Correlation ID shared by all LLM calls made within a single "
            "API invocation (one call_scope). Lets /v1/usage aggregate "
            "cost/duration per invocation by grouping on call_id."
        ),
    ),
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
    sa.Index(
        "idx_llm_calls_tenant_operation_call_id",
        "tenant_id",
        "operation",
        "call_id",
    ),
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


def _row_to_usage_metrics(row: sa.Row, *, suffix: str = "") -> UsageMetrics:
    """Build a ``UsageMetrics`` from a row's aggregate columns.

    The same row layout is reused for both per-LLM-call (suffix='')
    and per-invocation (suffix='_inv') aggregates within the same SELECT.

    Parameters
    ----------
    row : sa.Row
        Row from one of the GROUPING SETS queries.
    suffix : str
        Column-name suffix distinguishing per-invocation aggregates
        ('_inv') from per-LLM-call aggregates ('').

    Returns
    -------
    UsageMetrics
        Populated metrics, with zeros where the row has NULLs (the
        all-failed / no-records cases).
    """
    m = row._mapping
    total_calls = int(m[f"total_calls{suffix}"] or 0)
    failed_calls = int(m[f"failed_calls{suffix}"] or 0)
    return UsageMetrics(
        total_calls=total_calls,
        failed_calls=failed_calls,
        total_cost_usd=Decimal(str(m[f"total_cost_usd{suffix}"] or 0)),
        call_duration=_parse_distribution_ok(row, f"dur{suffix}"),
        input_tokens=_parse_token_stats_ok(row, f"inp{suffix}"),
        output_tokens=_parse_token_stats_ok(row, f"out{suffix}"),
    )


def _grouping_sets_clause(
    *,
    group_by_tenant: bool,
    group_by_operation: bool,
) -> sa.TextClause | None:
    """Build the ``GROUPING SETS (...)`` clause for the requested grouping.

    Returns None when neither tenant nor operation grouping is required
    (i.e. the original "single row totals" case for a single tenant
    without per-operation breakdown — which we never use in this PR, but
    keep as a defensive shortcut).
    """
    sets: list[str] = []
    if group_by_tenant and group_by_operation:
        sets.append("(tenant_id, operation)")
        sets.append("(tenant_id)")
        sets.append("()")
    elif group_by_operation:
        sets.append("(operation)")
        sets.append("()")
    elif group_by_tenant:
        sets.append("(tenant_id)")
        sets.append("()")
    else:
        return None
    return sa.text(f"GROUPING SETS ({', '.join(sets)})")


def _select_per_llm_call(
    base_pred: list,
    *,
    group_by_tenant: bool,
    group_by_operation: bool,
) -> sa.Select:
    """Per-LLM-call aggregation SELECT.

    Extension of the previous ``_select_totals``: emits ``GROUPING SETS``
    rows covering every requested level — per (tenant, operation), per
    tenant, and (when ``group_by_tenant=True``) the grand total. Each
    aggregate column is labelled with **no** suffix so a row can be
    consumed by ``_row_to_usage_metrics(row, suffix='')``.

    All-row counts (``total_calls``, ``failed_calls``) are unfiltered;
    cost, duration, and token distributions are filtered to ``status='ok'``.
    """
    ok_filter = llm_calls.c.status == "ok"
    err_filter = llm_calls.c.status == "error"

    cols: list[sa.ColumnElement] = []
    if group_by_tenant:
        cols.append(llm_calls.c.tenant_id)
    if group_by_operation:
        cols.append(llm_calls.c.operation)
    cols.extend(
        [
            sa.func.count().label("total_calls"),
            sa.func.count().filter(err_filter).label("failed_calls"),
            sa.func.coalesce(
                sa.func.sum(llm_calls.c.cost_usd).filter(ok_filter), 0
            ).label("total_cost_usd"),
            *_build_stats_columns(llm_calls.c.call_duration_ms, "dur", where=ok_filter),
            *_build_stats_columns(llm_calls.c.input_tokens, "inp", where=ok_filter),
            *_build_stats_columns(llm_calls.c.output_tokens, "out", where=ok_filter),
        ]
    )

    stmt = sa.select(*cols).where(*base_pred)
    grouping_sets = _grouping_sets_clause(
        group_by_tenant=group_by_tenant,
        group_by_operation=group_by_operation,
    )
    if grouping_sets is not None:
        stmt = stmt.group_by(grouping_sets)
    return stmt


def _select_per_invocation(
    base_pred: list,
    *,
    group_by_tenant: bool,
    group_by_operation: bool,
) -> sa.Select:
    """Per-invocation aggregation via a CTE grouping by ``call_id`` first.

    Inner step: one row per ``(tenant_id, operation, call_id)`` summing
    ``call_duration_ms``, ``input_tokens``, ``output_tokens``, ``cost_usd``
    across the LLM-call rows of that invocation, plus ``bool_and(status=
    'error')`` to flag all-failed invocations.

    Outer step: count all invocations (``total_calls_inv``), count
    all-failed invocations (``failed_calls_inv``), sum cost and compute
    distributions on the non-all-failed subset — i.e. distributions
    exclude all-failed invocations, mirroring the per-LLM-call
    "distributions exclude failures, counts include them" convention.

    Aggregate columns are labelled with the ``_inv`` suffix so the SELECT
    can later be combined with the per-LLM-call SELECT under one composition
    pass. ``GROUPING SETS`` matches ``_select_per_llm_call`` so the rows
    line up by ``(tenant_id, operation)`` keys.
    """
    per_invocation = (
        sa.select(
            llm_calls.c.tenant_id.label("tenant_id"),
            llm_calls.c.operation.label("operation"),
            llm_calls.c.call_id.label("call_id"),
            sa.func.sum(llm_calls.c.call_duration_ms).label("dur_sum"),
            sa.func.sum(llm_calls.c.input_tokens).label("inp_sum"),
            sa.func.sum(llm_calls.c.output_tokens).label("out_sum"),
            sa.func.sum(llm_calls.c.cost_usd).label("cost_sum"),
            sa.func.bool_and(llm_calls.c.status == "error").label("all_failed"),
        )
        .where(*base_pred)
        .group_by(
            llm_calls.c.tenant_id,
            llm_calls.c.operation,
            llm_calls.c.call_id,
        )
        .cte("per_invocation")
    )

    not_all_failed = per_invocation.c.all_failed.is_(False)
    is_all_failed = per_invocation.c.all_failed.is_(True)

    cols: list[sa.ColumnElement] = []
    if group_by_tenant:
        cols.append(per_invocation.c.tenant_id.label("tenant_id"))
    if group_by_operation:
        cols.append(per_invocation.c.operation.label("operation"))
    cols.extend(
        [
            sa.func.count().label("total_calls_inv"),
            sa.func.count().filter(is_all_failed).label("failed_calls_inv"),
            sa.func.coalesce(
                sa.func.sum(per_invocation.c.cost_sum).filter(not_all_failed),
                0,
            ).label("total_cost_usd_inv"),
            *_build_stats_columns(
                per_invocation.c.dur_sum, "dur_inv", where=not_all_failed
            ),
            *_build_stats_columns(
                per_invocation.c.inp_sum, "inp_inv", where=not_all_failed
            ),
            *_build_stats_columns(
                per_invocation.c.out_sum, "out_inv", where=not_all_failed
            ),
        ]
    )

    stmt = sa.select(*cols).select_from(per_invocation)
    grouping_sets = _grouping_sets_clause(
        group_by_tenant=group_by_tenant,
        group_by_operation=group_by_operation,
    )
    if grouping_sets is not None:
        stmt = stmt.group_by(grouping_sets)
    return stmt


def _zero_usage_metrics() -> UsageMetrics:
    """Zero ``UsageMetrics`` used as the fallback for missing roll-up rows."""
    return UsageMetrics(
        total_calls=0,
        failed_calls=0,
        total_cost_usd=Decimal("0"),
        call_duration=DistributionStats(avg=0, min=0, max=0, p5=0, p95=0),
        input_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
        output_tokens=TokenStats(avg=0, min=0, max=0, p5=0, p95=0, total=0),
    )


def _compose_usage_stats(
    tenant_id: str | None,
    inv_by_key: dict[tuple[str | None, str | None], UsageMetrics],
    llm_by_key: dict[tuple[str | None, str | None], UsageMetrics],
) -> UsageStats:
    """Assemble a ``UsageStats`` for one tenant (or grand total).

    Parameters
    ----------
    tenant_id : str | None
        The tenant whose stats to compose. ``None`` for the grand total.
    inv_by_key : dict
        Per-invocation metrics keyed by ``(tenant_id, operation)`` where
        either component may be ``None`` for the corresponding roll-up.
    llm_by_key : dict
        Per-LLM-call metrics keyed identically.

    Returns
    -------
    UsageStats
        Composed stats: per-invocation top-level fields, per-LLM-call
        ``llm_call_stats``, and a ``operations`` tuple sorted by cost
        desc / operation asc with empty-operation entries omitted.
    """
    # Per-operation rows: (tenant_id, op) where op is not None.
    operations: list[OperationStats] = []
    for (t, op), inv in inv_by_key.items():
        if t != tenant_id or op is None:
            continue
        if inv.total_calls == 0:
            continue
        llm = llm_by_key.get((tenant_id, op)) or _zero_usage_metrics()
        operations.append(
            OperationStats(
                operation=Operation(op),
                total_calls=inv.total_calls,
                failed_calls=inv.failed_calls,
                total_cost_usd=inv.total_cost_usd,
                call_duration=inv.call_duration,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                llm_call_stats=llm,
            )
        )

    operations.sort(key=lambda o: (-o.total_cost_usd, o.operation.value))

    tenant_inv = inv_by_key.get((tenant_id, None)) or _zero_usage_metrics()
    tenant_llm = llm_by_key.get((tenant_id, None)) or _zero_usage_metrics()
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=tenant_inv.total_calls,
        failed_calls=tenant_inv.failed_calls,
        total_cost_usd=tenant_inv.total_cost_usd,
        call_duration=tenant_inv.call_duration,
        input_tokens=tenant_inv.input_tokens,
        output_tokens=tenant_inv.output_tokens,
        llm_call_stats=tenant_llm,
        operations=tuple(operations),
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
                    call_id=record.call_id,
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
    ) -> UsageStats:
        """Per-invocation and per-LLM-call stats for one tenant.

        Runs two SELECTs (per-invocation via CTE on ``call_id``, plus
        per-LLM-call) each grouped by ``operation`` via ``GROUPING SETS``
        so a single round-trip per view returns both per-operation rows
        and the per-tenant roll-up. The two results are composed into a
        single ``UsageStats`` with a sorted ``operations`` tuple and an
        ``llm_call_stats`` block.

        Returns a zero ``UsageStats`` (``total_calls == 0``, empty
        ``operations``) when no rows match the window.
        """
        base_pred: list = [llm_calls.c.tenant_id == tenant_id]
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        async with _translate_db_errors(), self._session_factory() as session:
            inv_rows = (
                await session.execute(
                    _select_per_invocation(
                        base_pred,
                        group_by_tenant=False,
                        group_by_operation=True,
                    )
                )
            ).all()
            llm_rows = (
                await session.execute(
                    _select_per_llm_call(
                        base_pred,
                        group_by_tenant=False,
                        group_by_operation=True,
                    )
                )
            ).all()

        inv_by_key: dict[tuple[str | None, str | None], UsageMetrics] = {}
        for r in inv_rows:
            op = r._mapping.get("operation") if "operation" in r._mapping else None
            inv_by_key[(tenant_id, op)] = _row_to_usage_metrics(r, suffix="_inv")

        llm_by_key: dict[tuple[str | None, str | None], UsageMetrics] = {}
        for r in llm_rows:
            op = r._mapping.get("operation") if "operation" in r._mapping else None
            llm_by_key[(tenant_id, op)] = _row_to_usage_metrics(r, suffix="")

        return _compose_usage_stats(tenant_id, inv_by_key, llm_by_key)

    async def get_all_usage_stats(
        self,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[UsageStats]:
        """Per-tenant + grand-total stats with per-operation breakdown.

        Runs two SELECTs across all tenants — per-invocation via CTE on
        ``call_id`` and per-LLM-call — each with ``GROUPING SETS
        ((tenant_id, operation), (tenant_id), ())`` so each view returns
        every required level in a single round-trip (2 total queries
        regardless of tenant count). Composition then produces one
        ``UsageStats`` per tenant plus a grand-total entry
        (``tenant_id=None``).

        Tenants with zero per-invocation calls are filtered (preserves the
        existing contract); the grand-total entry is always emitted last
        even when empty.
        """
        base_pred: list = []
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        async with _translate_db_errors(), self._session_factory() as session:
            inv_rows = (
                await session.execute(
                    _select_per_invocation(
                        base_pred,
                        group_by_tenant=True,
                        group_by_operation=True,
                    )
                )
            ).all()
            llm_rows = (
                await session.execute(
                    _select_per_llm_call(
                        base_pred,
                        group_by_tenant=True,
                        group_by_operation=True,
                    )
                )
            ).all()

        inv_by_key: dict[tuple[str | None, str | None], UsageMetrics] = {}
        for r in inv_rows:
            m = r._mapping
            t = m.get("tenant_id") if "tenant_id" in m else None
            op = m.get("operation") if "operation" in m else None
            inv_by_key[(t, op)] = _row_to_usage_metrics(r, suffix="_inv")

        llm_by_key: dict[tuple[str | None, str | None], UsageMetrics] = {}
        for r in llm_rows:
            m = r._mapping
            t = m.get("tenant_id") if "tenant_id" in m else None
            op = m.get("operation") if "operation" in m else None
            llm_by_key[(t, op)] = _row_to_usage_metrics(r, suffix="")

        # Distinct tenants appearing in the per-invocation rollup rows.
        tenants = sorted({t for (t, op) in inv_by_key if t is not None and op is None})

        out: list[UsageStats] = []
        for t in tenants:
            stats = _compose_usage_stats(t, inv_by_key, llm_by_key)
            if stats.total_calls == 0:
                continue
            out.append(stats)

        # Grand total (tenant_id=None) — always emitted, even when empty,
        # matching the existing /v1/usage/all contract.
        out.append(_compose_usage_stats(None, inv_by_key, llm_by_key))
        return out
