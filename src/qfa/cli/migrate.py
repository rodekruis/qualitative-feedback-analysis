"""Run ``alembic upgrade head`` under a Postgres advisory lock.

Invoked from ``entrypoint.sh`` before uvicorn binds the port, and from
``make migrate`` in dev. Multi-replica safe: the session-scoped advisory
lock serialises concurrent migrators so non-winners wait for the winner
to finish before proceeding.

Run from the project root: the Alembic CLI resolves ``./alembic.ini``
from the current working directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from qfa.settings import DatabaseSettings

logger = logging.getLogger(__name__)

LOCK_KEY: int = 7424901234567890
"""Stable 64-bit integer key for the migration advisory lock."""


async def run_migrations(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url`` under an advisory lock.

    The lock is session-scoped: it is released automatically when the
    holding connection closes, so a crashed migrator cannot leave the
    keyspace permanently held.

    The Alembic CLI is invoked as a subprocess of the current Python
    interpreter (``sys.executable -m alembic``); this inherits the active
    venv without depending on ``PATH`` or ``uv``. The CLI resolves
    ``alembic.ini`` from the current working directory.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
            try:
                logger.info("Running alembic upgrade head")
                # Propagate db_url to alembic/env.py via DB_URL; the function
                # parameter is the single source of truth.
                subprocess.run(  # noqa: S603 — args are fully controlled, no user input
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    check=True,
                    env={**os.environ, "DB_URL": db_url},
                )
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY}
                )
    finally:
        await engine.dispose()


def main() -> int:
    """CLI entry point.

    Returns 0 on success (including the no-op case when usage tracking is
    disabled), or the alembic exit code on migration failure.
    """
    settings = DatabaseSettings()
    if not settings.track_usage:
        logger.info("DB_TRACK_USAGE is false; skipping migrations")
        return 0
    asyncio.run(run_migrations(settings.url))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        logger.error("alembic exited with code %d", exc.returncode)
        sys.exit(exc.returncode)
