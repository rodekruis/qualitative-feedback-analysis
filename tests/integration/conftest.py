"""Shared fixtures for Tier-2 integration tests against real PostgreSQL.

These tests are gated by ``@pytest.mark.integration`` and excluded from the
default ``pytest`` invocation (see ``pyproject.toml``). Run them with::

    make db-up               # start docker-compose Postgres
    make test-integration    # runs all integration + e2e tests

The DB URL defaults to the docker-compose service from ``docker-compose.yml``.
Override via the ``INTEGRATION_DB_URL`` env var to point at a different host.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    llm_calls,
)

DEFAULT_DB_URL = "postgresql+asyncpg://qfa:qfa@localhost:5432/qfa"


def integration_db_url() -> str:
    """Return the DB URL for integration tests."""
    return os.environ.get("INTEGRATION_DB_URL", DEFAULT_DB_URL)


async def _probe_or_fail(url: str) -> None:
    """Fail the test session if Postgres is unreachable at ``url``.

    Tests under ``-m "integration or e2e"`` are explicitly opted into;
    if the backing service isn't there, that is a setup error, not an
    applicability question. Skipping would let CI pass green when the
    Postgres service failed to start — a silent "tests look fine" trap.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        pytest.fail(
            f"Integration tests require Postgres at {url} (run `make db-up`). "
            f"Connection failed: {exc!s}",
            pytrace=False,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def pg_url() -> str:
    """Validate Postgres connectivity and return the DB URL."""
    url = integration_db_url()
    await _probe_or_fail(url)
    return url


@pytest_asyncio.fixture(scope="session")
async def pg_engine(pg_url: str) -> AsyncEngine:
    """Session-scoped engine; runs ``alembic upgrade head`` once.

    Invokes the same lock-guarded migration entry point that production
    uses (``qfa.cli.migrate.run_migrations``), including the in-process
    Alembic ``command.upgrade`` call on a lock-held DB connection.
    """
    from qfa.cli.migrate import run_migrations

    engine = create_async_engine(pg_url)
    await run_migrations(pg_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_repo(pg_engine: AsyncEngine):
    """A repository bound to the test engine, with ``llm_calls`` truncated."""
    async with pg_engine.begin() as conn:
        await conn.execute(sa.text("TRUNCATE TABLE llm_calls RESTART IDENTITY"))

    yield SqlAlchemyUsageRepository(create_session_factory(pg_engine))

    async with pg_engine.begin() as conn:
        await conn.execute(sa.text("TRUNCATE TABLE llm_calls RESTART IDENTITY"))


__all__ = ["llm_calls", "pg_engine", "pg_repo", "pg_url"]
