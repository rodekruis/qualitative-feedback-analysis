"""Tier-2 migration tests.

These verify the alembic upgrade/downgrade cycle and the advisory-lock
serialisation that protects multi-replica startup. Each test resets the
``public`` schema so they don't depend on test ordering.

Gated by ``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from qfa.adapters.migrations import upgrade_to_head

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def fresh_pg(pg_url: str):
    """Drop and recreate the ``public`` schema before each test."""
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    yield engine, pg_url
    async with engine.begin() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await engine.dispose()


class TestUpgradeFromEmpty:
    async def test_upgrades_to_head(self, fresh_pg):
        engine, url = fresh_pg
        await upgrade_to_head(engine, url)

        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "ORDER BY table_name"
                    )
                )
            ).fetchall()
        names = {row[0] for row in tables}
        assert "llm_calls" in names
        assert "alembic_version" in names

        async with engine.connect() as conn:
            cols = (
                await conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'llm_calls'"
                    )
                )
            ).fetchall()
        col_names = {row[0] for row in cols}
        assert {
            "tenant_id",
            "operation",
            "timestamp",
            "call_duration_ms",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "status",
            "error_class",
            "created_at",
        }.issubset(col_names)


class TestDowngradeUpgradeIdempotence:
    async def test_downgrade_to_base_then_upgrade_head_succeeds(self, fresh_pg):
        from alembic import command as alembic_command
        from qfa.adapters.migrations import _build_alembic_config

        engine, url = fresh_pg
        cfg = _build_alembic_config(url)

        await upgrade_to_head(engine, url)

        async with engine.connect() as conn:
            await conn.run_sync(
                lambda sync_conn: alembic_command.downgrade(
                    _set_conn(cfg, sync_conn), "base"
                )
            )

        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).fetchall()
        # After full downgrade, only alembic_version remains.
        assert {row[0] for row in tables} <= {"alembic_version"}

        await upgrade_to_head(engine, url)

        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).fetchall()
        assert "llm_calls" in {row[0] for row in tables}


class TestAdvisoryLockSerialises:
    async def test_two_concurrent_migrators_both_finish_at_head(self, fresh_pg):
        engine, url = fresh_pg

        # Each call must use its own engine — the lock is connection-scoped
        # within ``upgrade_to_head``, so concurrent calls need separate
        # connections to actually contend.
        engine_a = create_async_engine(url)
        engine_b = create_async_engine(url)
        try:
            await asyncio.gather(
                upgrade_to_head(engine_a, url),
                upgrade_to_head(engine_b, url),
            )
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

        async with engine.connect() as conn:
            rows = (
                await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).fetchall()
        assert len(rows) == 1


def _set_conn(cfg, sync_conn):
    cfg.attributes["connection"] = sync_conn
    return cfg
