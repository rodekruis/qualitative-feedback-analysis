"""Tests for auth-management API routes."""

import httpx
import pytest
import pytest_asyncio

from qfa.domain.errors import KeyAlreadyExistsError, TenantNotFoundError
from qfa.domain.models import TenantApiKey

from .conftest import FAKE_API_KEY, FAKE_SUPERUSER_KEY


class RecordingAuthOrchestrator:
    """Auth orchestrator test double with call recording."""

    def __init__(self, api_keys: list[TenantApiKey]) -> None:
        self._api_keys = api_keys
        self.add_tenant_calls: list[dict] = []
        self.delete_tenant_calls: list[str] = []
        self.get_tenants_calls: int = 0
        self.add_key_calls: list[dict] = []
        self.delete_key_calls: list[str] = []
        self.get_auth_keys_calls: list[str | None] = []

        self.raise_on_add_tenant: Exception | None = None
        self.raise_on_delete_tenant: Exception | None = None
        self.raise_on_add_key: Exception | None = None
        self.raise_on_delete_key: Exception | None = None

    async def validate_api_key(self, provided_key: str) -> TenantApiKey | None:
        for api_key in self._api_keys:
            if api_key.matches_key(provided_key):
                return api_key
        return None

    async def add_tenant(
        self,
        tenant_name: str,
        allows_superusers: bool = False,
    ) -> str:
        if self.raise_on_add_tenant is not None:
            raise self.raise_on_add_tenant
        self.add_tenant_calls.append(
            {
                "tenant_name": tenant_name,
                "allows_superusers": allows_superusers,
            }
        )
        return "tenant-created-1"

    async def delete_tenant(self, tenant_id: str) -> None:
        if self.raise_on_delete_tenant is not None:
            raise self.raise_on_delete_tenant
        self.delete_tenant_calls.append(tenant_id)

    async def get_tenants(self) -> list[dict]:
        self.get_tenants_calls += 1
        return [
            {"tenant_id": "tenant-a", "name": "Tenant A", "allows_superusers": False},
            {"tenant_id": "tenant-b", "name": "Tenant B", "allows_superusers": True},
        ]

    async def add_key(
        self,
        key_name: str,
        tenant_id: str,
        is_superuser: bool = False,
    ) -> tuple[str, str]:
        if self.raise_on_add_key is not None:
            raise self.raise_on_add_key
        key_id = "generated-key-id"
        api_key = "generated-api-key"
        self.add_key_calls.append(
            {
                "api_key": api_key,
                "key_id": key_id,
                "key_name": key_name,
                "tenant_id": tenant_id,
                "is_superuser": is_superuser,
            }
        )
        return key_id, api_key

    async def delete_key(self, key_id: str) -> None:
        if self.raise_on_delete_key is not None:
            raise self.raise_on_delete_key
        self.delete_key_calls.append(key_id)

    async def get_auth_keys(self, tenant_id: str | None = None) -> list[dict]:
        self.get_auth_keys_calls.append(tenant_id)
        records = [
            {
                "key_id": "k-1",
                "name": "Key One",
                "tenant_id": "tenant-a",
                "is_superuser": False,
            },
            {
                "key_id": "k-2",
                "name": "Key Two",
                "tenant_id": "tenant-b",
                "is_superuser": True,
            },
        ]
        if tenant_id is None:
            return records
        return [record for record in records if record["tenant_id"] == tenant_id]


@pytest.fixture
def auth_orchestrator_spy(fake_api_keys):
    return RecordingAuthOrchestrator(fake_api_keys)


@pytest_asyncio.fixture
async def auth_client(test_app, auth_orchestrator_spy):
    test_app.state.auth_orchestrator = auth_orchestrator_spy
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield client


def _auth_header(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestAuthManagementSuccess:
    @pytest.mark.asyncio
    async def test_add_tenant_201(self, auth_client, auth_orchestrator_spy):
        resp = await auth_client.post(
            "/v1/admin/tenants",
            json={"tenant_name": "Tenant Red", "allows_superusers": True},
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 201
        assert resp.json() == {"tenant_id": "tenant-created-1"}
        assert auth_orchestrator_spy.add_tenant_calls == [
            {"tenant_name": "Tenant Red", "allows_superusers": True}
        ]

    @pytest.mark.asyncio
    async def test_get_tenants_200(self, auth_client, auth_orchestrator_spy):
        resp = await auth_client.get(
            "/v1/admin/tenants",
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "tenants": [
                {
                    "tenant_id": "tenant-a",
                    "name": "Tenant A",
                    "allows_superusers": False,
                },
                {
                    "tenant_id": "tenant-b",
                    "name": "Tenant B",
                    "allows_superusers": True,
                },
            ]
        }
        assert auth_orchestrator_spy.get_tenants_calls == 1

    @pytest.mark.asyncio
    async def test_delete_tenant_204(self, auth_client, auth_orchestrator_spy):
        resp = await auth_client.delete(
            "/v1/admin/tenants/tenant-123",
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 204
        assert auth_orchestrator_spy.delete_tenant_calls == ["tenant-123"]

    @pytest.mark.asyncio
    async def test_add_key_201(self, auth_client, auth_orchestrator_spy):
        resp = await auth_client.post(
            "/v1/admin/keys",
            json={
                "key_name": "Ops Key",
                "tenant_id": "tenant-123",
                "is_superuser": False,
            },
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 201
        body = resp.json()
        assert isinstance(body["key_id"], str) and body["key_id"]
        assert isinstance(body["api_key"], str) and body["api_key"]
        assert len(auth_orchestrator_spy.add_key_calls) == 1
        call = auth_orchestrator_spy.add_key_calls[0]
        assert call["key_name"] == "Ops Key"
        assert call["tenant_id"] == "tenant-123"
        assert call["is_superuser"] is False
        assert call["key_id"] == body["key_id"]
        assert call["api_key"] == body["api_key"]

    @pytest.mark.asyncio
    async def test_delete_key_204(self, auth_client, auth_orchestrator_spy):
        resp = await auth_client.delete(
            "/v1/admin/keys/key-123",
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 204
        assert auth_orchestrator_spy.delete_key_calls == ["key-123"]

    @pytest.mark.asyncio
    async def test_get_auth_keys_200_with_tenant_filter(
        self,
        auth_client,
        auth_orchestrator_spy,
    ):
        resp = await auth_client.get(
            "/v1/admin/keys",
            params={"tenant_id": "tenant-a"},
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "auth_keys": [
                {
                    "key_id": "k-1",
                    "name": "Key One",
                    "tenant_id": "tenant-a",
                    "is_superuser": False,
                }
            ]
        }
        assert auth_orchestrator_spy.get_auth_keys_calls == ["tenant-a"]


class TestAuthManagementAuthorization:
    @pytest.mark.asyncio
    async def test_401_when_missing_auth_header(self, auth_client):
        resp = await auth_client.post(
            "/v1/admin/tenants",
            json={"tenant_name": "Tenant Red"},
        )

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_required"

    @pytest.mark.asyncio
    async def test_403_for_non_superuser(self, auth_client):
        resp = await auth_client.get(
            "/v1/admin/keys",
            headers=_auth_header(FAKE_API_KEY),
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"


class TestAuthManagementErrors:
    @pytest.mark.asyncio
    async def test_delete_tenant_not_found_maps_to_404(
        self,
        auth_client,
        auth_orchestrator_spy,
    ):
        auth_orchestrator_spy.raise_on_delete_tenant = TenantNotFoundError(
            "Tenant not found"
        )

        resp = await auth_client.delete(
            "/v1/admin/tenants/missing",
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_add_key_conflict_maps_to_409(
        self, auth_client, auth_orchestrator_spy
    ):
        auth_orchestrator_spy.raise_on_add_key = KeyAlreadyExistsError(
            "Key already exists"
        )

        resp = await auth_client.post(
            "/v1/admin/keys",
            json={
                "key_name": "Ops Key",
                "tenant_id": "tenant-123",
                "is_superuser": False,
            },
            headers=_auth_header(FAKE_SUPERUSER_KEY),
        )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
