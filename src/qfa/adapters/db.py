"""SQLAlchemy-based usage repository for LLM call tracking."""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from urllib.parse import quote

import sqlalchemy as sa
import sqlalchemy.event  # ensure sa.event is available to type checkers
from azure.identity import DefaultAzureCredential
from pydantic import SecretStr
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qfa.domain.errors import (
    KeyAlreadyExistsError,
    KeyNotFoundError,
    TenantDoesNotAllowSuperUsersError,
    TenantNotFoundError,
    UsageRepositoryUnavailableError,
)
from qfa.domain.models import (
    DistributionStats,
    LLMCallRecord,
    TenantApiKey,
    TokenStats,
    UsageStats,
)
from qfa.domain.ports import AuthLookupPort, AuthManagementPort, UsageRepositoryPort
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

tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("allows_superusers", sa.Boolean, nullable=False, default=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)

keys = sa.Table(
    "keys",
    metadata,
    sa.Column("key_id", sa.String(255), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("hashed_key", sa.String(64), nullable=False),
    sa.Column(
        "tenant_id",
        sa.String(255),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("is_superuser", sa.Boolean, nullable=False, default=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Index("idx_keys_tenant_id", "tenant_id"),
    sa.Index("idx_keys_hashed_key", "hashed_key"),
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


def _select_totals(
    base_pred: list,
    *,
    group_by_tenant: bool,
) -> sa.Select:
    """Build the totals + ok-only distributions SELECT shared by both stats paths.

    All-row aggregates (``total_calls``, ``failed_calls``) are unfiltered,
    while cost, duration, and token aggregates use ``FILTER (WHERE
    status='ok')`` so a single SELECT mixes both scopes.

    When ``group_by_tenant`` is True, the SELECT includes ``tenant_id``
    and groups via ``GROUPING SETS ((tenant_id), ())`` so a single
    round-trip returns per-tenant rows plus a grand-total roll-up
    (``tenant_id IS NULL``), ordered alphabetically with the roll-up
    last. Postgres-only.
    """
    ok_filter = llm_calls.c.status == "ok"
    err_filter = llm_calls.c.status == "error"

    cols: list[sa.ColumnElement] = []
    if group_by_tenant:
        cols.append(llm_calls.c.tenant_id)
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
    if group_by_tenant:
        stmt = stmt.group_by(sa.text("GROUPING SETS ((tenant_id), ())")).order_by(
            llm_calls.c.tenant_id.asc().nulls_last()
        )
    return stmt


class SqlAlchemyUsageRepository(
    UsageRepositoryPort, AuthLookupPort, AuthManagementPort
):
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
    ) -> UsageStats:
        """Aggregate stats for a single tenant within an optional time window.

        Cost, distributions, and token totals scope to ``status='ok'`` rows.
        ``total_calls`` and ``failed_calls`` count all rows in the window.
        """
        base_pred: list = [llm_calls.c.tenant_id == tenant_id]
        if from_ is not None:
            base_pred.append(llm_calls.c.timestamp >= from_)
        if to is not None:
            base_pred.append(llm_calls.c.timestamp < to)

        async with _translate_db_errors(), self._session_factory() as session:
            row = (
                await session.execute(_select_totals(base_pred, group_by_tenant=False))
            ).one()

        return _row_to_usage_stats(tenant_id, row)

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

        async with _translate_db_errors(), self._session_factory() as session:
            rows = (
                await session.execute(_select_totals(base_pred, group_by_tenant=True))
            ).all()

        # GROUPING SETS always emits the () roll-up row even with zero source
        # rows; the prior contract was to return [] in that case, so skip rows
        # whose total_calls is 0.
        return [
            _row_to_usage_stats(r._mapping["tenant_id"], r)
            for r in rows
            if int(r._mapping["total_calls"]) != 0
        ]

    async def validate_api_key(self, provided_key: str) -> TenantApiKey | None:
        """Validate an API key against hashed keys stored in DB."""
        stmt = sa.select(
            keys.c.key_id,
            keys.c.name,
            keys.c.hashed_key,
            keys.c.tenant_id,
            keys.c.is_superuser,
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()

        match: TenantApiKey | None = None
        for row in rows:
            tenant_key = TenantApiKey(
                key_id=str(row.key_id),
                name=str(row.name),
                hashed_key=SecretStr(str(row.hashed_key)),
                tenant_id=str(row.tenant_id),
                is_superuser=bool(row.is_superuser),
            )
            if tenant_key.matches_key(provided_key):
                match = tenant_key

        return match

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[dict]:
        """Get API key metadata for one tenant or all tenants."""
        stmt = sa.select(
            keys.c.key_id,
            keys.c.name,
            keys.c.tenant_id,
            keys.c.is_superuser,
        ).order_by(keys.c.tenant_id.asc(), keys.c.key_id.asc())
        if tenant_id is not None:
            stmt = stmt.where(keys.c.tenant_id == tenant_id)

        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()

        return [
            {
                "key_id": str(row.key_id),
                "name": str(row.name),
                "tenant_id": str(row.tenant_id),
                "is_superuser": bool(row.is_superuser),
            }
            for row in rows
        ]

    async def add_tenant(
        self, tenant_name: str, allows_superusers: bool = False
    ) -> str:
        """Create and persist a tenant record, returning its id."""
        tenant_id = str(uuid.uuid4())
        async with self._session_factory() as session:
            await session.execute(
                tenants.insert().values(
                    tenant_id=tenant_id,
                    name=tenant_name,
                    allows_superusers=allows_superusers,
                )
            )
            await session.commit()
        return tenant_id

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete a tenant and all related keys."""
        async with self._session_factory() as session:
            # Keep behavior deterministic across backends/tests even when
            # FK cascades are not enforced (e.g. sqlite without PRAGMA).
            await session.execute(keys.delete().where(keys.c.tenant_id == tenant_id))
            result = cast(
                CursorResult,
                await session.execute(
                    tenants.delete().where(tenants.c.tenant_id == tenant_id)
                ),
            )
            if result.rowcount == 0:
                await session.rollback()
                raise TenantNotFoundError(f"Tenant '{tenant_id}' not found")
            await session.commit()

    async def add_key(
        self,
        api_key: str,
        key_id: str,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> str:
        """Persist a new API key for a tenant."""
        async with self._session_factory() as session:
            tenant_row = (
                await session.execute(
                    sa.select(tenants.c.allows_superusers).where(
                        tenants.c.tenant_id == tenant_id
                    )
                )
            ).one_or_none()
            if tenant_row is None:
                await session.rollback()
                raise TenantNotFoundError(f"Tenant '{tenant_id}' not found")

            if is_superuser and not bool(tenant_row.allows_superusers):
                await session.rollback()
                raise TenantDoesNotAllowSuperUsersError(
                    f"Tenant '{tenant_id}' does not allow superuser keys"
                )

            try:
                await session.execute(
                    keys.insert().values(
                        key_id=key_id,
                        name=key_name,
                        hashed_key=TenantApiKey.hash_key(api_key),
                        tenant_id=tenant_id,
                        is_superuser=is_superuser,
                    )
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KeyAlreadyExistsError(
                    f"Key with id '{key_id}' already exists"
                ) from exc

        return key_id

    async def delete_key(self, key_id: str) -> None:
        """Delete an API key by id."""
        async with self._session_factory() as session:
            result = cast(
                CursorResult,
                await session.execute(keys.delete().where(keys.c.key_id == key_id)),
            )
            if result.rowcount == 0:
                await session.rollback()
                raise KeyNotFoundError(f"Key with id '{key_id}' not found")
            await session.commit()
