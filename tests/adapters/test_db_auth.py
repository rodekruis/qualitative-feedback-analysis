"""Tests for SQLAlchemy auth lookup/management methods on SqlAlchemyUsageRepository."""

import asyncio

import pytest
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


@pytest.fixture
def repo_with_engine(tmp_path):
    pytest.importorskip("aiosqlite")
    db_path = tmp_path / "auth-test.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    asyncio.run(_init())

    repo = SqlAlchemyUsageRepository(create_session_factory(engine))
    yield repo, engine

    asyncio.run(engine.dispose())


class TestPortConformance:
    def test_explicitly_inherits_auth_ports(self):
        assert AuthLookupPort in SqlAlchemyUsageRepository.__mro__
        assert AuthManagementPort in SqlAlchemyUsageRepository.__mro__


class TestTenantManagement:
    def test_add_tenant_returns_id(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = repo.add_tenant("Tenant A")

        assert isinstance(tenant_id, str)
        assert tenant_id != ""

    def test_delete_missing_tenant_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(TenantNotFoundError):
            repo.delete_tenant("missing-tenant")


class TestKeyManagement:
    def test_add_key_and_validate_round_trip(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = repo.add_tenant("Tenant A")

        returned_key_id = repo.add_key(
            api_key="secret-abc",
            key_id="a1",
            key_name="Primary key",
            tenant_id=tenant_id,
        )

        matched = repo.validate_api_key("secret-abc")
        assert returned_key_id == "a1"
        assert matched is not None
        assert matched.key_id == "a1"
        assert matched.name == "Primary key"
        assert matched.tenant_id == tenant_id
        assert matched.is_superuser is False

    def test_add_key_with_unknown_tenant_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(TenantNotFoundError):
            repo.add_key(
                api_key="secret-abc",
                key_id="a1",
                key_name="Primary key",
                tenant_id="missing-tenant",
            )

    def test_duplicate_key_id_raises(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = repo.add_tenant("Tenant A")
        repo.add_key(
            api_key="secret-abc",
            key_id="a1",
            key_name="Primary key",
            tenant_id=tenant_id,
        )

        with pytest.raises(KeyAlreadyExistsError):
            repo.add_key(
                api_key="another-secret",
                key_id="a1",
                key_name="Duplicate key id",
                tenant_id=tenant_id,
            )

    def test_superuser_requires_tenant_permission(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_id = repo.add_tenant("Tenant A", allows_superusers=False)

        with pytest.raises(TenantDoesNotAllowSuperUsersError):
            repo.add_key(
                api_key="secret-abc",
                key_id="su-1",
                key_name="Superuser key",
                tenant_id=tenant_id,
                is_superuser=True,
            )

    def test_delete_missing_key_raises(self, repo_with_engine):
        repo, _ = repo_with_engine

        with pytest.raises(KeyNotFoundError):
            repo.delete_key("missing-key")


class TestLookupAndListing:
    def test_get_auth_keys_filters_by_tenant(self, repo_with_engine):
        repo, _ = repo_with_engine
        tenant_a = repo.add_tenant("Tenant A")
        tenant_b = repo.add_tenant("Tenant B")

        repo.add_key("secret-a1", "a1", "A1", tenant_a)
        repo.add_key("secret-a2", "a2", "A2", tenant_a)
        repo.add_key("secret-b1", "b1", "B1", tenant_b)

        all_keys = repo.get_auth_keys()
        only_a = repo.get_auth_keys(tenant_id=tenant_a)

        assert len(all_keys) == 3
        assert len(only_a) == 2
        assert all(k["tenant_id"] == tenant_a for k in only_a)

    def test_delete_tenant_cascades_keys(self, repo_with_engine):
        repo, engine = repo_with_engine
        tenant_id = repo.add_tenant("Tenant A")
        repo.add_key("secret-a1", "a1", "A1", tenant_id)

        repo.delete_tenant(tenant_id)

        assert repo.get_auth_keys(tenant_id=tenant_id) == []

        async def _count_keys() -> int:
            async with engine.connect() as conn:
                return int(
                    (
                        await conn.execute(sa.select(sa.func.count()).select_from(keys))
                    ).scalar_one()
                )

        assert asyncio.run(_count_keys()) == 0
