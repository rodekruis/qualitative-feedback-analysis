"""App-startup migration runner guarded by a Postgres advisory lock.

Used when ``DB_TRACK_USAGE=true``. Multiple replicas may all attempt to
``alembic upgrade head`` at startup; the advisory lock serialises them.
The lock is session-scoped, so a crashed migrator releases on connection
close.
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command as alembic_command

logger = logging.getLogger(__name__)

LLM_CALLS_MIGRATION_LOCK_KEY: int = 7424901234567890
"""Stable 64-bit integer key for the migration advisory lock."""


async def upgrade_to_head(engine: AsyncEngine, db_url: str) -> None:
    """Run ``alembic upgrade head`` under a Postgres advisory lock.

    Parameters
    ----------
    engine : AsyncEngine
        Async engine used to acquire the lock and run the migration.
    db_url : str
        URL passed to Alembic's config (must include async driver).
    """
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("SELECT pg_advisory_lock(:k)"),
            {"k": LLM_CALLS_MIGRATION_LOCK_KEY},
        )
        try:
            cfg = _build_alembic_config(db_url)
            await conn.run_sync(lambda sync_conn: _run_upgrade(cfg, sync_conn))
        finally:
            await conn.execute(
                sa.text("SELECT pg_advisory_unlock(:k)"),
                {"k": LLM_CALLS_MIGRATION_LOCK_KEY},
            )

    async with engine.connect() as probe:
        await probe.execute(sa.text("SELECT 1"))


def _build_alembic_config(db_url: str) -> AlembicConfig:
    repo_root = Path(__file__).resolve().parents[3]
    cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _run_upgrade(cfg: AlembicConfig, sync_conn) -> None:  # noqa: ANN001
    cfg.attributes["connection"] = sync_conn
    alembic_command.upgrade(cfg, "head")
