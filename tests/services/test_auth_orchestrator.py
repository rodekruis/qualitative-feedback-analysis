"""Tests for the auth orchestrator service."""

import pytest
from pydantic import SecretStr

from qfa.domain.errors import AuthenticationError
from qfa.domain.models import TenantApiKey
from qfa.domain.ports import AuthLookupPort, AuthManagementPort
from qfa.services.auth_orchestrator import AuthOrchestrator

pytestmark = pytest.mark.asyncio


def _make_tenant_api_key(
    *,
    key_id: str = "key-1",
    name: str = "Primary key",
    key_value: str = "secret-key",
    tenant_id: str = "tenant-1",
    is_superuser: bool = False,
) -> TenantApiKey:
    return TenantApiKey(
        key_id=key_id,
        name=name,
        hashed_key=SecretStr(TenantApiKey.hash_key(key_value)),
        tenant_id=tenant_id,
        is_superuser=is_superuser,
    )


class FakeAuthLookupPort(AuthLookupPort):
    def __init__(self, validation_responses=None, auth_keys=None):
        self.validation_responses = list(validation_responses or [])
        self.auth_keys = list(auth_keys or [])
        self.validate_calls = []
        self.get_auth_keys_calls = []

    async def validate_api_key(self, provided_key: str) -> TenantApiKey | None:
        self.validate_calls.append(provided_key)
        if self.validation_responses:
            return self.validation_responses.pop(0)
        return None

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[dict]:
        self.get_auth_keys_calls.append(tenant_id)
        return list(self.auth_keys)


class FakeAuthManagementPort(AuthManagementPort):
    def __init__(self, tenant_id_to_return: str = "tenant-created"):
        self.tenant_id_to_return = tenant_id_to_return
        self.key_id_to_return = "key-created"
        self.api_key_to_return = "api-key-created"
        self.add_tenant_calls = []
        self.delete_tenant_calls = []
        self.get_tenants_calls = 0
        self.add_key_calls = []
        self.delete_key_calls = []

    async def add_tenant(
        self, tenant_name: str, allows_superusers: bool = False
    ) -> str:
        self.add_tenant_calls.append(
            {
                "tenant_name": tenant_name,
                "allows_superusers": allows_superusers,
            }
        )
        return self.tenant_id_to_return

    async def delete_tenant(self, tenant_id: str) -> None:
        self.delete_tenant_calls.append(tenant_id)

    async def get_tenants(self) -> list[dict]:
        self.get_tenants_calls += 1
        return [
            {"tenant_id": "t-1", "name": "Tenant One", "allows_superusers": False},
        ]

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> tuple[str, str]:
        self.add_key_calls.append(
            {
                "key_name": key_name,
                "tenant_id": tenant_id,
                "is_superuser": is_superuser,
            }
        )
        return self.key_id_to_return, self.api_key_to_return

    async def delete_key(self, key_id: str) -> None:
        self.delete_key_calls.append(key_id)


class TestInit:
    async def test_requires_at_least_one_lookup_port(self):
        management_port = FakeAuthManagementPort()

        with pytest.raises(
            ValueError,
            match="at least one auth_lookup_port",
        ):
            AuthOrchestrator([], management_port)


class TestValidateApiKey:
    async def test_returns_first_matching_lookup_result(self):
        expected = _make_tenant_api_key(key_id="match", tenant_id="tenant-42")
        first_lookup = FakeAuthLookupPort(validation_responses=[None])
        second_lookup = FakeAuthLookupPort(validation_responses=[expected])
        third_lookup = FakeAuthLookupPort(
            validation_responses=[_make_tenant_api_key(key_id="unused")]
        )
        orchestrator = AuthOrchestrator(
            [first_lookup, second_lookup, third_lookup],
            FakeAuthManagementPort(),
        )

        result = await orchestrator.validate_api_key(("provided-secret"))

        assert result == expected
        assert first_lookup.validate_calls == ["provided-secret"]
        assert second_lookup.validate_calls == ["provided-secret"]
        assert third_lookup.validate_calls == []

    async def test_raises_authentication_error_when_no_lookup_matches(self):
        first_lookup = FakeAuthLookupPort(validation_responses=[None])
        second_lookup = FakeAuthLookupPort(validation_responses=[None])
        orchestrator = AuthOrchestrator(
            [first_lookup, second_lookup],
            FakeAuthManagementPort(),
        )

        with pytest.raises(
            AuthenticationError,
            match="API key does not match any known key",
        ):
            await orchestrator.validate_api_key(("missing-secret"))

        assert first_lookup.validate_calls == ["missing-secret"]
        assert second_lookup.validate_calls == ["missing-secret"]


class TestTenantManagement:
    async def test_add_tenant_delegates_to_management_port(self):
        management_port = FakeAuthManagementPort(tenant_id_to_return="tenant-99")
        orchestrator = AuthOrchestrator([FakeAuthLookupPort()], management_port)

        tenant_id = await orchestrator.add_tenant("Tenant Red", allows_superusers=True)

        assert tenant_id == "tenant-99"
        assert management_port.add_tenant_calls == [
            {"tenant_name": "Tenant Red", "allows_superusers": True}
        ]

    async def test_delete_tenant_delegates_to_management_port(self):
        management_port = FakeAuthManagementPort()
        orchestrator = AuthOrchestrator([FakeAuthLookupPort()], management_port)

        await orchestrator.delete_tenant("tenant-7")

        assert management_port.delete_tenant_calls == ["tenant-7"]

    async def test_get_tenants_delegates_to_management_port(self):
        management_port = FakeAuthManagementPort()
        orchestrator = AuthOrchestrator([FakeAuthLookupPort()], management_port)

        result = await orchestrator.get_tenants()

        assert result == [
            {"tenant_id": "t-1", "name": "Tenant One", "allows_superusers": False}
        ]
        assert management_port.get_tenants_calls == 1


class TestKeyManagement:
    async def test_add_key_delegates_all_arguments(self):
        management_port = FakeAuthManagementPort()
        orchestrator = AuthOrchestrator([FakeAuthLookupPort()], management_port)

        key_id, api_key = await orchestrator.add_key(
            key_name="Operations",
            tenant_id="tenant-7",
            is_superuser=True,
        )

        assert management_port.add_key_calls == [
            {
                "key_name": "Operations",
                "tenant_id": "tenant-7",
                "is_superuser": True,
            }
        ]
        assert key_id == "key-created"
        assert api_key == "api-key-created"

    async def test_delete_key_delegates_to_management_port(self):
        management_port = FakeAuthManagementPort()
        orchestrator = AuthOrchestrator([FakeAuthLookupPort()], management_port)

        await orchestrator.delete_key("key-8")

        assert management_port.delete_key_calls == ["key-8"]


class TestGetAuthKeys:
    async def test_aggregates_auth_keys_from_all_lookup_ports(self):
        first_lookup = FakeAuthLookupPort(
            auth_keys=[{"key_id": "a1", "tenant_id": "tenant-a"}]
        )
        second_lookup = FakeAuthLookupPort(
            auth_keys=[
                {"key_id": "b1", "tenant_id": "tenant-a"},
                {"key_id": "b2", "tenant_id": "tenant-a"},
            ]
        )
        orchestrator = AuthOrchestrator(
            [first_lookup, second_lookup],
            FakeAuthManagementPort(),
        )

        result = await orchestrator.get_auth_keys(tenant_id="tenant-a")

        assert result == [
            {"key_id": "a1", "tenant_id": "tenant-a"},
            {"key_id": "b1", "tenant_id": "tenant-a"},
            {"key_id": "b2", "tenant_id": "tenant-a"},
        ]
        assert first_lookup.get_auth_keys_calls == ["tenant-a"]
        assert second_lookup.get_auth_keys_calls == ["tenant-a"]
