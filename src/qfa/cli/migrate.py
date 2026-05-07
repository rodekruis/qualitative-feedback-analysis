"""Run ``alembic upgrade head`` under a Postgres advisory lock.

Invoked from ``entrypoint.sh`` before uvicorn binds the port, and from
``make migrate`` in dev. Multi-replica safe: the session-scoped advisory
lock serialises concurrent migrators so non-winners wait for the winner
to finish before proceeding.

Run from the project root: Alembic resolves ``./alembic.ini`` from the
current working directory.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from qfa.adapters.db import create_async_engine_from_settings
from qfa.settings import DatabaseSettings

logger = logging.getLogger(__name__)

LOCK_KEY: int = 7424901234567890
"""Stable 64-bit integer key for the migration advisory lock."""


def _alembic_upgrade_head(sync_connection):  # noqa: ANN001
    """Run Alembic upgrade using an existing SQLAlchemy sync connection."""
    config = Config("alembic.ini")
    config.attributes["connection"] = sync_connection
    command.upgrade(config, "head")


async def run_migrations(db: DatabaseSettings | str) -> None:
    """Run ``alembic upgrade head`` under an advisory lock.

    The lock is session-scoped: it is released automatically when the
    holding connection closes, so a crashed migrator cannot leave the
    keyspace permanently held.

    Parameters
    ----------
    db : DatabaseSettings | str
        Either full DB settings (preferred, supports Entra token auth)
        or an explicit SQLAlchemy URL (used by integration tests).
    """
    if isinstance(db, DatabaseSettings):
        engine = create_async_engine_from_settings(db)
    else:
        engine = create_async_engine(db)

    try:
        autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with autocommit_engine.connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
            try:
                logger.info("Running alembic upgrade head")
                await conn.run_sync(_alembic_upgrade_head)
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY}
                )
    finally:
        await engine.dispose()


def main() -> int:
    """CLI entry point.

    Returns 0 on success (including the no-op case when usage tracking is
    disabled).
    """
    settings = DatabaseSettings()
    if not settings.track_usage:
        logger.info("DB_TRACK_USAGE is false; skipping migrations")
        return 0
    asyncio.run(run_migrations(settings))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Migration run failed")
        sys.exit(1)
