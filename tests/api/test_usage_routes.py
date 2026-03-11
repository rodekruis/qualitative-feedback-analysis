"""Tests for usage tracking API endpoints."""

import pytest
import pytest_asyncio

from qfa.domain.models import (
    DistributionStats,
    TokenStats,
    UsageStats,
)

FAKE_API_KEY = "test-key-abc123"
FAKE_SUPERUSER_KEY = "superuser-key-xyz789"

pytestmark = pytest.mark.asyncio


def _make_usage_stats(tenant_id="tenant-test", total_calls=5):
    return UsageStats(
        tenant_id=tenant_id,
        total_calls=total_calls,
        call_duration=DistributionStats(avg=100, min=50, max=200, p5=55, p95=190),
        input_tokens=TokenStats(
            avg=500, min=100, max=1000, p5=120, p95=950, total=2500
        ),
        output_tokens=TokenStats(avg=200, min=50, max=400, p5=60, p95=380, total=1000),
    )


class FakeUsageRepository:
    """Fake usage repository for testing."""

    def __init__(self, stats=None, all_stats=None):
        self._stats = stats
        self._all_stats = all_stats or []

    async def record_call(self, record):
        pass

    async def get_usage_stats(self, tenant_id):
        return self._stats

    async def get_all_usage_stats(self):
        return self._all_stats


class TestUsageEndpointDisabled:
    async def test_returns_503_when_tracking_disabled(self, client):
        resp = await client.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 503

    async def test_all_returns_503_when_tracking_disabled(self, test_app, client):
        from qfa.domain.models import TenantApiKey

        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
            )
        )
        resp = await client.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 503


class TestUsageEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        import httpx

        repo = FakeUsageRepository(stats=_make_usage_stats())
        test_app.state.usage_repo = repo

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c

    async def test_returns_200_with_stats(self, client_with_repo):
        resp = await client_with_repo.get(
            "/v1/usage",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 5
        assert data["tenant_id"] == "tenant-test"

    async def test_requires_authentication(self, client_with_repo):
        resp = await client_with_repo.get("/v1/usage")
        assert resp.status_code == 401


class TestUsageAllEndpoint:
    @pytest_asyncio.fixture
    async def client_with_repo(self, test_app):
        import httpx

        from qfa.domain.models import TenantApiKey

        # Add superuser key
        test_app.state.api_keys.append(
            TenantApiKey(
                name="superuser",
                key=FAKE_SUPERUSER_KEY,
                tenant_id="admin",
                is_superuser=True,
            )
        )
        all_stats = [
            _make_usage_stats(tenant_id="t1", total_calls=3),
            _make_usage_stats(tenant_id="t2", total_calls=7),
            UsageStats(
                tenant_id=None,
                total_calls=10,
                call_duration=DistributionStats(
                    avg=100, min=50, max=200, p5=55, p95=190
                ),
                input_tokens=TokenStats(
                    avg=500, min=100, max=1000, p5=120, p95=950, total=5000
                ),
                output_tokens=TokenStats(
                    avg=200, min=50, max=400, p5=60, p95=380, total=2000
                ),
            ),
        ]
        test_app.state.usage_repo = FakeUsageRepository(all_stats=all_stats)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app),
            base_url="http://test",
        ) as c:
            yield c

    async def test_returns_200_for_superuser(self, client_with_repo):
        resp = await client_with_repo.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_SUPERUSER_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tenants"]) == 2
        assert data["total"]["total_calls"] == 10

    async def test_returns_403_for_non_superuser(self, client_with_repo):
        resp = await client_with_repo.get(
            "/v1/usage/all",
            headers={"Authorization": f"Bearer {FAKE_API_KEY}"},
        )
        assert resp.status_code == 403
