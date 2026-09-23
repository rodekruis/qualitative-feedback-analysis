"""Tests for the Postgres re-grant CLI (``qfa.cli.db_grant``).

No live-Postgres variant exists on purpose: ``pgaadauth_create_principal_with_oid``
is an Azure Flexible Server extension and is absent from the ``postgres:16``
service container CI uses. What is worth guarding here is the statement
sequence and the identifier validation — the CLI interpolates role names into
SQL because identifiers cannot be bound as parameters.
"""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from qfa.cli.db_grant import grant_admin

pytestmark = pytest.mark.asyncio

OID = "11111111-2222-3333-4444-555555555555"


async def _grant(conn: "_FakeConnection", **kwargs) -> None:
    await grant_admin(cast(AsyncConnection, conn), **kwargs)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConnection:
    """Records executed statements; answers the role-exists probe."""

    def __init__(self, role_exists: bool = False):
        self.statements: list[str] = []
        self.params: list[dict | None] = []
        self._role_exists = role_exists

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params)
        if "pg_roles" in str(statement):
            return _FakeResult((1,) if self._role_exists else None)
        return _FakeResult(None)


async def test_creates_principal_and_transfers_the_old_role():
    conn = _FakeConnection(role_exists=False)

    await _grant(
        conn,
        principal_name="qfa-dev-db-admin",
        object_id=OID,
        from_role="qfa-dev-backend",
    )

    assert conn.statements == [
        "SELECT 1 FROM pg_roles WHERE rolname = :name",
        "SELECT pgaadauth_create_principal_with_oid(:name, :oid, 'service', true, false)",
        'GRANT "qfa-dev-backend" TO "qfa-dev-db-admin"',
        'REASSIGN OWNED BY "qfa-dev-backend" TO "qfa-dev-db-admin"',
        'GRANT ALL ON SCHEMA public TO "qfa-dev-db-admin"',
    ]
    assert conn.params[1] == {"name": "qfa-dev-db-admin", "oid": OID}


async def test_existing_principal_skips_creation_but_still_grants():
    conn = _FakeConnection(role_exists=True)

    await _grant(conn, principal_name="qfa-dev-db-admin", object_id=OID)

    assert not any("pgaadauth" in s for s in conn.statements)
    assert conn.statements[-1] == 'GRANT ALL ON SCHEMA public TO "qfa-dev-db-admin"'


@pytest.mark.parametrize(
    "principal_name",
    ['qfa"; DROP SCHEMA public; --', "qfa dev", "1-qfa", ""],
)
async def test_rejects_invalid_role_names_before_emitting_sql(principal_name):
    conn = _FakeConnection()

    with pytest.raises(ValueError):
        await _grant(conn, principal_name=principal_name, object_id=OID)

    assert conn.statements == []


async def test_rejects_non_uuid_object_id_before_emitting_sql():
    conn = _FakeConnection()

    with pytest.raises(ValueError):
        await _grant(conn, principal_name="qfa-dev-db-admin", object_id="not-a-uuid")

    assert conn.statements == []
