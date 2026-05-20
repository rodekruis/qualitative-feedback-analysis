"""Database engine, session factory, SQL schema, and auth adapter.

The ``llm_calls`` table declared here backs the usage repository
(:mod:`qfa.adapters.usage_repository`); the ``tenants`` and ``keys``
tables back :class:`SQLAlchemyAuthAdapter`. The engine + session helpers
are the composition root the API layer uses to wire repositories at
startup.
"""

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from urllib.parse import quote

import sqlalchemy as sa
import sqlalchemy.event  # ensure sa.event is available to type checkers
from azure.identity import DefaultAzureCredential
from pydantic import SecretStr
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
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
)
from qfa.domain.models import (
    AuthKeyInfo,
    KeyCreationResponse,
    TenantApiKey,
    TenantInfo,
)
from qfa.domain.ports import AuthLookupPort, AuthManagementPort
from qfa.settings import DatabaseSettings

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

tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column(
        "allows_superusers",
        sa.Boolean,
        nullable=False,
        default=False,
        comment=(
            "Allows tenant-level control over whether superuser keys are permitted. "
            "Enforced in the application layer since it's not a simple FK constraint."
        ),
    ),
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
    sa.Column("hashed_key", sa.String(length=128), nullable=False),
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


class SQLAlchemyAuthAdapter(AuthLookupPort, AuthManagementPort):
    """Auth adapter backed by SQLAlchemy and PostgreSQL."""

    def __init__(self, session_factory: Callable[..., AsyncSession]) -> None:
        self._session_factory = session_factory

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

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[AuthKeyInfo]:
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
            AuthKeyInfo(
                key_id=str(row.key_id),
                name=str(row.name),
                tenant_id=str(row.tenant_id),
                is_superuser=bool(row.is_superuser),
            )
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

    async def get_tenants(self) -> list[TenantInfo]:
        """Return metadata for all tenants."""
        stmt = sa.select(
            tenants.c.tenant_id,
            tenants.c.name,
            tenants.c.allows_superusers,
        ).order_by(tenants.c.tenant_id.asc())

        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()

        return [
            TenantInfo(
                tenant_id=str(row.tenant_id),
                name=str(row.name),
                allows_superusers=bool(row.allows_superusers),
            )
            for row in rows
        ]

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> KeyCreationResponse:
        """Persist a new API key for a tenant."""
        key_id = str(uuid.uuid4())
        api_key = secrets.token_urlsafe(32)

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
                tenant_api_key = TenantApiKey(
                    key_id=key_id,
                    name=key_name,
                    key=SecretStr(api_key),
                    hashed_key=None,  # type: ignore[ty:invalid-argument-type]
                    tenant_id=tenant_id,
                    is_superuser=is_superuser,
                )
                await session.execute(
                    keys.insert().values(
                        key_id=key_id,
                        name=key_name,
                        hashed_key=tenant_api_key.hashed_key.get_secret_value(),
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

            return KeyCreationResponse(key_id=key_id, api_key=api_key)

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
