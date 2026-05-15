"""Tests for SQLAlchemy auth lookup/management methods on SqlAlchemyUsageRepository."""

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from qfa.adapters.db import (
    SqlAlchemyUsageRepository,
    create_session_factory,
    keys,
    metadata,
)
from qfa.domain.errors import (
    KeyAlreadyExistsError,
    KeyNotFoundError,
    TenantDoesNotAllowSuperUsersError,
    TenantNotFoundError,
)
from qfa.domain.ports import AuthLookupPort, AuthManagementPort

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def repo_with_engine(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = tmp_path / "auth-test.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    repo = SqlAlchemyUsageRepository(create_session_factory(engine))
    yield repo, engine

    await engine.dispose()


class TestPortConformance:
    async def test_explicitly_inherits_auth_ports(self):
        assert AuthLookupPort in SqlAlchemyUsageRepository.__mro__
        assert AuthManagementPort in SqlAlchemyUsageRepository.__mro__


class TestTenantManagement:
    async def test_add_tenant_returns_id(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = await repo.add_tenant("Tenant A")

        assert isinstance(tenant_id, str)
        assert tenant_id != ""

    async def test_delete_missing_tenant_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(TenantNotFoundError):
            await repo.delete_tenant("missing-tenant")

    async def test_get_tenants_returns_all(self, repo_with_engine):
        repo, _ = repo_with_engine
        await repo.add_tenant("Tenant A", allows_superusers=False)
        await repo.add_tenant("Tenant B", allows_superusers=True)

        result = await repo.get_tenants()

        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"Tenant A", "Tenant B"}
        for record in result:
            assert set(record.keys()) == {"tenant_id", "name", "allows_superusers"}


class TestKeyManagement:
    async def test_add_key_and_validate_round_trip(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = await repo.add_tenant("Tenant A")

        returned_key_id = await repo.add_key(
            api_key="secret-abc",
            key_id="a1",
            key_name="Primary key",
            tenant_id=tenant_id,
        )

        matched = await repo.validate_api_key("secret-abc")
        assert returned_key_id == "a1"
        assert matched is not None
        assert matched.key_id == "a1"
        assert matched.name == "Primary key"
        assert matched.tenant_id == tenant_id
        assert matched.is_superuser is False

    async def test_add_key_with_unknown_tenant_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(TenantNotFoundError):
            await repo.add_key(
                api_key="secret-abc",
                key_id="a1",
                key_name="Primary key",
                tenant_id="missing-tenant",
            )

    async def test_duplicate_key_id_raises(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = await repo.add_tenant("Tenant A")
        await repo.add_key(
            api_key="secret-abc",
            key_id="a1",
            key_name="Primary key",
            tenant_id=tenant_id,
        )

        with pytest.raises(KeyAlreadyExistsError):
            await repo.add_key(
                api_key="another-secret",
                key_id="a1",
                key_name="Duplicate key id",
                tenant_id=tenant_id,
            )

    async def test_superuser_requires_tenant_permission(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = await repo.add_tenant("Tenant A", allows_superusers=False)

        with pytest.raises(TenantDoesNotAllowSuperUsersError):
            await repo.add_key(
                api_key="secret-abc",
                key_id="su-1",
                key_name="Superuser key",
                tenant_id=tenant_id,
                is_superuser=True,
            )

    async def test_delete_missing_key_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(KeyNotFoundError):
            await repo.delete_key("missing-key")


class TestLookupAndListing:
    async def test_get_auth_keys_filters_by_tenant(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_a = await repo.add_tenant("Tenant A")
        tenant_b = await repo.add_tenant("Tenant B")

        await repo.add_key("secret-a1", "a1", "A1", tenant_a)
        await repo.add_key("secret-a2", "a2", "A2", tenant_a)
        await repo.add_key("secret-b1", "b1", "B1", tenant_b)

        all_keys = await repo.get_auth_keys()
        only_a = await repo.get_auth_keys(tenant_id=tenant_a)

        assert len(all_keys) == 3
        assert len(only_a) == 2
        assert all(k["tenant_id"] == tenant_a for k in only_a)

    async def test_delete_tenant_cascades_keys(self, repo_with_engine):
        repo, engine = repo_with_engine
        tenant_id = await repo.add_tenant("Tenant A")
        await repo.add_key("secret-a1", "a1", "A1", tenant_id)

        await repo.delete_tenant(tenant_id)

        assert await repo.get_auth_keys(tenant_id=tenant_id) == []

        async def _count_keys() -> int:
            async with engine.connect() as conn:
                return int(
                    (
                        await conn.execute(sa.select(sa.func.count()).select_from(keys))
                    ).scalar_one()
                )

        assert await _count_keys() == 0
