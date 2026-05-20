"""Database engine, session factory, and SQL schema for the qfa adapter layer.

The ``llm_calls`` table declared here backs the usage repository
(:mod:`qfa.adapters.usage_repository`); the engine + session helpers are
the composition root the API layer uses to wire repositories at startup.
"""

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import sqlalchemy as sa
import sqlalchemy.event  # ensure sa.event is available to type checkers
from azure.identity import DefaultAzureCredential
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
