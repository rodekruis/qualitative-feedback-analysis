"""Tier-2 migration tests.

These verify the alembic upgrade/downgrade cycle and the advisory-lock
serialisation that protects multi-replica startup. Each test resets the
``public`` schema so they don't depend on test ordering.

The concurrent-migrator test spawns real OS subprocesses (one per
simulated replica) rather than coroutines, because the production
contention happens between separate Python processes, and a single
``asyncio`` loop running ``subprocess.run`` would serialise them
trivially without ever exercising the lock.

Gated by ``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from qfa.cli.migrate import run_migrations

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
        await run_migrations(url)

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
        engine, url = fresh_pg

        await run_migrations(url)

        env = {**os.environ, "DB_URL": url}
        subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "base"],
            env=env,
            check=True,
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

        await run_migrations(url)

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

        # Spawn two real OS subprocesses to simulate two replicas booting
        # in parallel. The session-scoped advisory lock should serialise
        # them so both end with a single alembic_version row at head.
        env = {**os.environ, "DB_TRACK_USAGE": "true", "DB_URL": url}
        proc_a = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "qfa.cli.migrate", env=env
        )
        proc_b = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "qfa.cli.migrate", env=env
        )
        rc_a, rc_b = await asyncio.gather(proc_a.wait(), proc_b.wait())
        assert rc_a == 0, "first migrator did not exit cleanly"
        assert rc_b == 0, "second migrator did not exit cleanly"

        async with engine.connect() as conn:
            rows = (
                await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).fetchall()
        assert len(rows) == 1
