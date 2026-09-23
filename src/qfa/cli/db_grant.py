"""Provision an Entra principal as a PostgreSQL admin and hand over the old role's estate.

Run **from inside the App Service container, as the identity that is currently
the server's Entra administrator** — Azure only lets the admin create further
principals, and the database has no public network access, so ``az webapp ssh``
is the only way in. The runtime image ships no ``psql`` and no ``az``, which is
why this is a CLI module and not a ``.sql`` file under ``scripts/``::

    python -m qfa.cli.db_grant --principal-name qfa-dev-db-admin \
        --object-id <uuid> --from-role qfa-dev-backend

Idempotent: an existing role is left alone and only the grants are re-run.
With ``--from-role`` the new principal is granted the old role *and* takes
ownership of everything it owns, so nothing is revoked and a rollback to the
old admin still works. The cutover runbook is
``docs/operations/how-to.md``; the reasoning is ADR-023.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from qfa.adapters.db import create_async_engine_from_settings
from qfa.settings import DatabaseSettings

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
"""Azure identity names, conservatively. SQL identifiers cannot be bound as
parameters, so anything reaching a quoted identifier must match this first."""


def _quote_identifier(value: str) -> str:
    """Validate and double-quote a SQL identifier.

    Raises
    ------
    ValueError
        If ``value`` does not match :data:`_IDENTIFIER`.
    """
    if not _IDENTIFIER.match(value):
        raise ValueError(
            f"{value!r} is not a valid role name — expected letters, digits, "
            "'-' and '_' only, starting with a letter"
        )
    return f'"{value}"'


async def grant_admin(
    conn: AsyncConnection,
    *,
    principal_name: str,
    object_id: str,
    from_role: str | None = None,
) -> None:
    """Make ``principal_name`` a Postgres admin bound to ``object_id``.

    ``conn`` must already be authenticated as the server's current Entra
    administrator. When ``from_role`` is given, its privileges are granted and
    its owned objects are reassigned to the new principal.

    Raises
    ------
    ValueError
        If ``principal_name``/``from_role`` is not a valid identifier, or
        ``object_id`` is not a UUID. Raised before any SQL is emitted.
    """
    quoted_name = _quote_identifier(principal_name)
    quoted_from = _quote_identifier(from_role) if from_role else None
    uuid.UUID(object_id)  # reject anything that is not an Entra object ID

    existing = (
        await conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": principal_name},
        )
    ).first()

    if existing is None:
        logger.info(
            "Creating Postgres principal %s (oid %s)", principal_name, object_id
        )
        await conn.execute(
            text(
                "SELECT pgaadauth_create_principal_with_oid("
                ":name, :oid, 'service', true, false)"
            ),
            {"name": principal_name, "oid": object_id},
        )
    else:
        logger.info(
            "Principal %s already exists — re-running grants only", principal_name
        )

    if quoted_from is not None:
        logger.info("Granting %s to %s", from_role, principal_name)
        await conn.execute(text(f"GRANT {quoted_from} TO {quoted_name}"))
        logger.info("Reassigning objects owned by %s to %s", from_role, principal_name)
        await conn.execute(text(f"REASSIGN OWNED BY {quoted_from} TO {quoted_name}"))

    logger.info("Granting schema public to %s", principal_name)
    await conn.execute(text(f"GRANT ALL ON SCHEMA public TO {quoted_name}"))


async def run(principal_name: str, object_id: str, from_role: str | None) -> None:
    """Connect with the container's own DB settings and apply the grants."""
    engine = create_async_engine_from_settings(DatabaseSettings())
    try:
        async with engine.begin() as conn:
            await grant_admin(
                conn,
                principal_name=principal_name,
                object_id=object_id,
                from_role=from_role,
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns 0 on success.
    """
    parser = argparse.ArgumentParser(
        description="Make an Entra principal a PostgreSQL admin."
    )
    parser.add_argument(
        "--principal-name",
        required=True,
        help="Entra identity name; becomes the Postgres role name",
    )
    parser.add_argument(
        "--object-id",
        required=True,
        help="Principal (object) ID of that identity, as a UUID",
    )
    parser.add_argument(
        "--from-role",
        default=None,
        help="Existing role whose privileges and owned objects transfer over",
    )
    args = parser.parse_args(argv)

    asyncio.run(run(args.principal_name, args.object_id, args.from_role))
    logger.info("Done — %s is a database admin", args.principal_name)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        sys.exit(main())
    except Exception as exc:
        logger.error("Grant failed: %s", exc)
        sys.exit(1)
